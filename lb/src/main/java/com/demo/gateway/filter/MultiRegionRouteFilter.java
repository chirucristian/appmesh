package com.demo.gateway.filter;

import com.demo.gateway.config.GatewayServicesProperties;
import com.demo.gateway.config.GatewayServicesProperties.RegionConfig;
import com.demo.gateway.config.GatewayServicesProperties.ServiceConfig;
import com.demo.gateway.health.CircuitBreakerRegistry;
import com.demo.gateway.health.RegionHealthMonitor;
import io.netty.channel.ChannelOption;
import org.reactivestreams.Publisher;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.cloud.gateway.filter.GatewayFilter;
import org.springframework.cloud.gateway.filter.GatewayFilterChain;
import org.springframework.core.io.buffer.DataBuffer;
import org.springframework.core.io.buffer.DataBufferUtils;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpMethod;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.MediaType;
import org.springframework.http.client.reactive.ReactorClientHttpConnector;
import org.springframework.http.server.reactive.ServerHttpRequest;
import org.springframework.http.server.reactive.ServerHttpResponse;
import org.springframework.web.reactive.function.BodyExtractors;
import org.springframework.web.reactive.function.client.ClientResponse;
import org.springframework.web.reactive.function.client.WebClient;
import org.springframework.web.server.ServerWebExchange;
import reactor.core.publisher.Flux;
import reactor.core.publisher.Mono;
import reactor.netty.http.client.HttpClient;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

import static org.springframework.cloud.gateway.support.ServerWebExchangeUtils.setAlreadyRouted;

/**
 * Core gateway filter that proxies incoming requests to the best available region
 * for a given service.
 * <p>
 * For every incoming request the filter:
 * <ol>
 *   <li>Redirects paths without a trailing slash to their directory form</li>
 *   <li>Strips the service path prefix (e.g. {@code /police-service/api/v1/} → {@code /api/v1/})</li>
 *   <li>Iterates through the service's configured regions in order</li>
 *   <li>Skips regions whose circuit breaker is OPEN</li>
 *   <li>Proxies the request (including body) via {@link WebClient}</li>
 *   <li>On 5xx or connection error → records a failure and retries the next region</li>
 *   <li>On success → records a success and forwards the response to the caller</li>
 * </ol>
 */
public class MultiRegionRouteFilter implements GatewayFilter {

    private static final Logger log = LoggerFactory.getLogger(MultiRegionRouteFilter.class);

    /**
     * Hop-by-hop headers that must NOT be forwarded.
     */
    private static final Set<String> HOP_BY_HOP = Set.of(
            "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
            "te", "trailer", "transfer-encoding", "upgrade"
    );

    private final String serviceName;
    private final ServiceConfig serviceConfig;
    private final CircuitBreakerRegistry circuitBreaker;
    private final WebClient webClient;
    private final Duration readTimeout;
    private final String localRegion;

    public MultiRegionRouteFilter(String serviceName,
                                  GatewayServicesProperties properties,
                                  CircuitBreakerRegistry circuitBreaker) {
        this.serviceName = serviceName;
        this.serviceConfig = properties.getServices().get(serviceName);
        this.circuitBreaker = circuitBreaker;
        this.readTimeout = Duration.ofMillis(properties.getTimeout().getReadTimeoutMs());
        this.localRegion = resolveLocalRegion();

        HttpClient httpClient = HttpClient.create()
                .option(ChannelOption.CONNECT_TIMEOUT_MILLIS, properties.getTimeout().getConnectTimeoutMs())
                .responseTimeout(readTimeout);

        this.webClient = WebClient.builder()
                .clientConnector(new ReactorClientHttpConnector(httpClient))
                .build();
    }

    // ── GatewayFilter ──────────────────────────────────────────────────

    @Override
    public Mono<Void> filter(ServerWebExchange exchange, GatewayFilterChain chain) {
        // Mark the exchange as already routed so the default NettyRoutingFilter is skipped.
        setAlreadyRouted(exchange);

        ServerHttpRequest request = exchange.getRequest();
        String originalPath = request.getURI().getRawPath();
        if (!originalPath.endsWith("/")) {
            return redirectToTrailingSlash(exchange, request, originalPath);
        }

        String remainingPath = stripServicePrefix(originalPath);
        String query = request.getURI().getRawQuery();
        HttpMethod method = request.getMethod();
        HttpHeaders headers = sanitiseRequestHeaders(request.getHeaders());

        // Cache the request body so it can be replayed across region retries.
        Mono<byte[]> cachedBody = cacheRequestBody(request);

        List<RegionConfig> regions = orderRegionsForThisGateway(serviceConfig.getRegions());

        return cachedBody.flatMap(body ->
                tryRegion(exchange, regions, remainingPath, query, method, headers, body, 0)
        );
    }

    // ── Region iteration with failover ─────────────────────────────────

    private Mono<Void> tryRegion(ServerWebExchange exchange,
                                 List<RegionConfig> regions,
                                 String path, String query,
                                 HttpMethod method, HttpHeaders headers,
                                 byte[] body, int attempt) {

        if (attempt >= regions.size()) {
            log.error("All {} regions exhausted for service [{}]", regions.size(), serviceName);
            return writeError(exchange, 503, "All regions unavailable for service: " + serviceName);
        }

        RegionConfig region = regions.get(attempt);
        String cbKey = RegionHealthMonitor.regionKey(serviceName, region.getName());
        boolean multiRegion = regions.size() > 1;

        // Fast-fail only when there is somewhere else to go. For single-region
        // services, avoid circuit-breaker dance and just try the one backend.
        if (multiRegion && !circuitBreaker.isCallPermitted(cbKey)) {
            log.debug("Circuit breaker OPEN for {} – skipping", cbKey);
            return tryRegion(exchange, regions, path, query, method, headers, body, attempt + 1);
        }

        String targetUri = buildTargetUri(region.getUrl(), path, query);
        log.debug("Routing [{}] {} → {} (region={}, attempt={})",
                serviceName, method, targetUri, region.getName(), attempt);

        WebClient.RequestBodySpec spec = webClient.method(method)
                .uri(targetUri)
                .headers(h -> h.addAll(headers));

        // Build the request spec (attach body for POST/PUT/PATCH)
        WebClient.RequestHeadersSpec<?> requestSpec;
        if (body.length > 0 && requiresBody(method)) {
            requestSpec = spec.bodyValue(body);
        } else {
            requestSpec = spec;
        }

        // Body MUST be consumed/released inside the exchangeToMono callback
        return requestSpec
                .exchangeToMono(clientResponse -> {
                    HttpStatusCode status = clientResponse.statusCode();
                    if (multiRegion && status.is5xxServerError()) {
                        log.warn("Region {} returned {} – failing over", cbKey, status);
                        circuitBreaker.recordFailure(cbKey);
                        return clientResponse.releaseBody()
                                .then(Mono.<Void>error(new RegionFailedException(cbKey)));
                    }
                    // Success (or 4xx – client's problem, not a region failure)
                    if (multiRegion) {
                        circuitBreaker.recordSuccess(cbKey);
                    }
                    return writeProxiedResponse(exchange, clientResponse);
                })
                .onErrorResume(ex -> {
                    if (!multiRegion) {
                        log.warn("Single-region backend {} failed – {}", cbKey, ex.getMessage());
                        return writeError(exchange, 503, "Service region unavailable: " + serviceName);
                    }
                    if (!(ex instanceof RegionFailedException)) {
                        log.warn("Connection error to region {} – {}", cbKey, ex.getMessage());
                        circuitBreaker.recordFailure(cbKey);
                    }
                    return tryRegion(exchange, regions, path, query, method, headers, body, attempt + 1);
                });
    }

    /** Marker exception used to distinguish a 5xx failover from a connection error. */
    private static class RegionFailedException extends RuntimeException {
        RegionFailedException(String regionKey) {
            super("Region failed: " + regionKey);
        }
    }

    private String resolveLocalRegion() {
        String region = System.getenv("AWS_REGION");
        if (region == null || region.isBlank()) {
            region = System.getenv("AWS_DEFAULT_REGION");
        }
        return region == null ? "" : region.trim();
    }

    private List<RegionConfig> orderRegionsForThisGateway(List<RegionConfig> configured) {
        if (configured == null || configured.size() <= 1 || localRegion.isBlank()) {
            return configured;
        }
        List<RegionConfig> ordered = new ArrayList<>(configured.size());
        for (RegionConfig region : configured) {
            if (localRegion.equals(region.getName())) {
                ordered.add(region);
            }
        }
        for (RegionConfig region : configured) {
            if (!localRegion.equals(region.getName())) {
                ordered.add(region);
            }
        }
        return ordered;
    }

    // ── Helpers ────────────────────────────────────────────────────────

    private Mono<Void> writeProxiedResponse(ServerWebExchange exchange, ClientResponse clientResponse) {
        ServerHttpResponse response = exchange.getResponse();
        response.setStatusCode(clientResponse.statusCode());

        // Copy response headers, skipping hop-by-hop headers
        clientResponse.headers().asHttpHeaders().forEach((name, values) -> {
            if (!HOP_BY_HOP.contains(name.toLowerCase())) {
                response.getHeaders().put(name, values);
            }
        });

        Flux<DataBuffer> body = clientResponse.body(BodyExtractors.toDataBuffers());
        return response.writeWith(body);
    }

    /**
     * Reads the full request body into a byte[] so it can be replayed for each retry.
     * Returns an empty byte[] for bodiless requests.
     */
    private Mono<byte[]> cacheRequestBody(ServerHttpRequest request) {
        return DataBufferUtils.join(request.getBody())
                .map(buf -> {
                    byte[] bytes = new byte[buf.readableByteCount()];
                    buf.read(bytes);
                    DataBufferUtils.release(buf);
                    return bytes;
                })
                .defaultIfEmpty(new byte[0]);
    }

    /**
     * Strips the service name prefix from the incoming path.
     * <pre>
     *   /police-service           → /
     *   /police-service/          → /
     *   /police-service/api/v1    → /api/v1
     * </pre>
     */
    private String stripServicePrefix(String path) {
        String prefix = "/" + serviceName;
        if (path.equals(prefix) || path.equals(prefix + "/")) {
            return "/";
        }
        if (path.startsWith(prefix + "/")) {
            return path.substring(prefix.length());
        }
        // Shouldn't happen if the route predicate is correct, but be safe:
        return path;
    }

    private static String buildTargetUri(String baseUrl, String path, String query) {
        // Strip any trailing slash from baseUrl to avoid double slashes
        String base = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
        StringBuilder sb = new StringBuilder(base).append(path);
        if (query != null && !query.isEmpty()) {
            sb.append('?').append(query);
        }
        return sb.toString();
    }

    private static HttpHeaders sanitiseRequestHeaders(HttpHeaders original) {
        HttpHeaders h = new HttpHeaders();
        original.forEach((name, values) -> {
            if (!HOP_BY_HOP.contains(name.toLowerCase())
                    && !name.equalsIgnoreCase(HttpHeaders.HOST)) {
                h.put(name, values);
            }
        });
        return h;
    }

    private static boolean requiresBody(HttpMethod method) {
        return method == HttpMethod.POST
                || method == HttpMethod.PUT
                || method == HttpMethod.PATCH;
    }

    private Mono<Void> redirectToTrailingSlash(ServerWebExchange exchange,
                                               ServerHttpRequest request,
                                               String path) {
        ServerHttpResponse response = exchange.getResponse();
        response.setRawStatusCode(308);
        String location = path + "/";
        String query = request.getURI().getRawQuery();
        if (query != null && !query.isEmpty()) {
            location += "?" + query;
        }
        response.getHeaders().set(HttpHeaders.LOCATION, location);
        return response.setComplete();
    }

    private static Mono<Void> writeError(ServerWebExchange exchange, int status, String message) {
        ServerHttpResponse response = exchange.getResponse();
        response.setRawStatusCode(status);
        response.getHeaders().setContentType(MediaType.APPLICATION_JSON);
        String json = "{\"error\":\"" + message.replace("\"", "\\\"") + "\"}";
        DataBuffer buf = response.bufferFactory().wrap(json.getBytes());
        return response.writeWith(Mono.just(buf));
    }
}
