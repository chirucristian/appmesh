package com.demo.gateway.config;

import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.fasterxml.jackson.dataformat.yaml.YAMLFactory;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.cloud.gateway.event.RefreshRoutesEvent;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.context.event.EventListener;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import software.amazon.awssdk.core.ResponseBytes;
import software.amazon.awssdk.http.urlconnection.UrlConnectionHttpClient;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.s3.S3Client;
import software.amazon.awssdk.services.s3.model.GetObjectResponse;
import software.amazon.awssdk.services.s3.model.HeadObjectResponse;

/**
 * Loads this regional gateway's routing configuration from an {@code application.yml}
 * object stored in S3, so the topology can change without redeploying the gateway.
 * <p>
 * The object is fetched once at startup and then re-checked on a fixed interval
 * (default 30 s). An {@code ETag} comparison skips the download + rebind when the
 * object is unchanged. On a real change the {@link GatewayServicesProperties} bean
 * is repopulated in place — {@link com.demo.gateway.health.RegionHealthMonitor} and
 * the status controller read it live — and a {@link RefreshRoutesEvent} is published
 * so {@link DynamicRouteLocator} rebuilds the proxy routes.
 * <p>
 * Credentials are resolved via the AWS default provider chain: an EC2 instance role
 * when deployed on AWS, or {@code AWS_*} env vars / {@code ~/.aws/credentials} locally.
 * No keys live in the application.
 * <p>
 * Expected object shape (same as the bundled {@code application.yml}):
 * <pre>
 * gateway:
 *   services:
 *     ambulance-service:
 *       paths: [ /ambulance-service/** ]
 *       health-check-path: /status
 *       regions:
 *         - { name: eu-central-1, url: http://... }
 *   circuit-breaker: { ... }   # optional overrides
 *   health-check:    { ... }   # optional overrides
 *   timeout:         { ... }   # optional overrides
 * </pre>
 */
@Component
public class S3RouteConfigLoader {

    private static final Logger log = LoggerFactory.getLogger(S3RouteConfigLoader.class);

    private final GatewayServicesProperties properties;
    private final ApplicationEventPublisher events;

    private final boolean enabled;
    private final String bucket;
    private final String key;

    private final S3Client s3;
    private final ObjectMapper yaml;

    /** ETag of the last successfully applied object; null until first load. */
    private volatile String lastEtag;

    public S3RouteConfigLoader(GatewayServicesProperties properties,
                               ApplicationEventPublisher events,
                               @Value("${route-sync.enabled:false}") boolean enabled,
                               @Value("${route-sync.bucket:}") String bucket,
                               @Value("${route-sync.key:}") String key,
                               @Value("${route-sync.region:us-east-1}") String region) {
        this.properties = properties;
        this.events = events;
        this.enabled = enabled;
        this.bucket = bucket;
        this.key = resolveRegionalKey(key);

        this.yaml = new ObjectMapper(new YAMLFactory())
                .setPropertyNamingStrategy(PropertyNamingStrategies.KEBAB_CASE)
                .configure(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES, false);

        this.s3 = enabled
                ? S3Client.builder()
                    .region(Region.of(region))
                    .httpClientBuilder(UrlConnectionHttpClient.builder())
                    .build()
                : null;

        if (enabled) {
            log.info("S3 route sync ENABLED — source s3://{}/{} (region={})", bucket, key, region);
        } else {
            log.info("S3 route sync DISABLED — using bundled application.yml routes");
        }
    }

    private String resolveRegionalKey(String configuredKey) {
        String gatewayRegion = System.getenv("AWS_REGION");
        if (gatewayRegion == null || gatewayRegion.isBlank()) {
            gatewayRegion = System.getenv("AWS_DEFAULT_REGION");
        }
        if (gatewayRegion == null || gatewayRegion.isBlank()) {
            return configuredKey;
        }
        String trimmedRegion = gatewayRegion.trim();
        if (configuredKey.contains("{region}")) {
            return configuredKey.replace("{region}", trimmedRegion);
        }
        if (configuredKey.endsWith("/")) {
            return configuredKey + trimmedRegion + "/application.yml";
        }
        if ("conf/lb/application.yml".equals(configuredKey)) {
            return "conf/lb/" + trimmedRegion + "/application.yml";
        }
        return configuredKey;
    }

    @EventListener(ApplicationReadyEvent.class)
    public void loadOnStartup() {
        if (enabled) {
            refresh("startup");
        }
    }

    @Scheduled(fixedDelayString = "${route-sync.poll-interval-ms:30000}",
               initialDelayString = "${route-sync.poll-interval-ms:30000}")
    public void poll() {
        if (enabled) {
            refresh("poll");
        }
    }

    private synchronized void refresh(String trigger) {
        try {
            HeadObjectResponse head = s3.headObject(b -> b.bucket(bucket).key(key));
            String etag = head.eTag();
            if (etag != null && etag.equals(lastEtag)) {
                log.debug("S3 route config unchanged (etag={}, trigger={})", etag, trigger);
                return;
            }

            ResponseBytes<GetObjectResponse> object =
                    s3.getObjectAsBytes(b -> b.bucket(bucket).key(key));
            GatewayServicesProperties parsed = parse(object.asByteArray());
            apply(parsed);
            lastEtag = etag;

            events.publishEvent(new RefreshRoutesEvent(this));
            log.info("Gateway routes reloaded from s3://{}/{} — {} service(s) [{}] (trigger={})",
                    bucket, key, parsed.getServices().size(),
                    String.join(", ", parsed.getServices().keySet()), trigger);
        } catch (Exception e) {
            // Keep serving the previously-applied config on any failure.
            log.warn("Failed to load gateway routes from s3://{}/{} ({}): {}",
                    bucket, key, trigger, e.toString());
        }
    }

    private GatewayServicesProperties parse(byte[] body) throws Exception {
        JsonNode root = yaml.readTree(body);
        JsonNode gateway = root.get("gateway");
        if (gateway == null || gateway.get("services") == null) {
            throw new IllegalArgumentException("object has no 'gateway.services' section");
        }
        return yaml.treeToValue(gateway, GatewayServicesProperties.class);
    }

    /** Repopulate the shared properties bean in place so live readers pick it up. */
    private void apply(GatewayServicesProperties parsed) {
        properties.setServices(parsed.getServices());
        if (parsed.getCircuitBreaker() != null) {
            properties.setCircuitBreaker(parsed.getCircuitBreaker());
        }
        if (parsed.getHealthCheck() != null) {
            properties.setHealthCheck(parsed.getHealthCheck());
        }
        if (parsed.getTimeout() != null) {
            properties.setTimeout(parsed.getTimeout());
        }
    }
}
