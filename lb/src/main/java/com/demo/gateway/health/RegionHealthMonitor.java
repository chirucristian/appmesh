package com.demo.gateway.health;

import com.demo.gateway.config.GatewayServicesProperties;
import com.demo.gateway.config.GatewayServicesProperties.RegionConfig;
import com.demo.gateway.config.GatewayServicesProperties.ServiceConfig;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.web.reactive.function.client.WebClient;

import java.time.Duration;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Periodically pings every configured region's health-check endpoint
 * and feeds results into the {@link CircuitBreakerRegistry}.
 * <p>
 * A region whose health-check fails is recorded as a circuit-breaker failure;
 * a region whose health-check succeeds is recorded as a success.
 * This way the circuit breaker will automatically recover (OPEN → HALF_OPEN → CLOSED)
 * once the service comes back up.
 */
@Component
public class RegionHealthMonitor {

    private static final Logger log = LoggerFactory.getLogger(RegionHealthMonitor.class);

    private final GatewayServicesProperties properties;
    private final CircuitBreakerRegistry circuitBreaker;
    private final WebClient webClient;
    private final Duration healthTimeout;

    /** Set of region keys known to be unreachable (for status reporting). */
    private final Set<String> unhealthyRegions = ConcurrentHashMap.newKeySet();

    public RegionHealthMonitor(GatewayServicesProperties properties,
                               CircuitBreakerRegistry circuitBreaker) {
        this.properties = properties;
        this.circuitBreaker = circuitBreaker;
        this.healthTimeout = Duration.ofSeconds(properties.getHealthCheck().getTimeoutSeconds());
        this.webClient = WebClient.builder().build();
    }

    /**
     * Scheduled health-check loop.
     * The fixed-delay is configured via {@code gateway.health-check.interval-seconds}
     * (defaults to 15 000 ms).
     */
    @Scheduled(fixedDelayString = "${gateway.health-check.interval-seconds:15}000",
               initialDelayString = "${gateway.health-check.interval-seconds:15}000")
    public void checkAll() {
        for (Map.Entry<String, ServiceConfig> entry : properties.getServices().entrySet()) {
            String serviceName = entry.getKey();
            ServiceConfig svc = entry.getValue();
            if (svc.getRegions() == null || svc.getRegions().size() <= 1) {
                continue;
            }
            for (RegionConfig region : svc.getRegions()) {
                checkRegion(serviceName, svc, region);
            }
        }
    }

    private void checkRegion(String serviceName, ServiceConfig svc, RegionConfig region) {
        String key = regionKey(serviceName, region.getName());
        String healthUrl = region.getUrl() + svc.getHealthCheckPath();

        webClient.get()
                .uri(healthUrl)
                .retrieve()
                .toBodilessEntity()
                .timeout(healthTimeout)
                .subscribe(
                        resp -> {
                            if (resp.getStatusCode().is2xxSuccessful()) {
                                log.debug("Health OK  : {} → {}", key, healthUrl);
                                circuitBreaker.recordSuccess(key);
                                unhealthyRegions.remove(key);
                            } else {
                                log.warn("Health FAIL: {} → {} (status {})", key, healthUrl, resp.getStatusCode());
                                circuitBreaker.recordFailure(key);
                                unhealthyRegions.add(key);
                            }
                        },
                        ex -> {
                            log.warn("Health FAIL: {} → {} ({})", key, healthUrl, ex.getMessage());
                            circuitBreaker.recordFailure(key);
                            unhealthyRegions.add(key);
                        }
                );
    }

    /** Check whether a region is considered healthy right now. */
    public boolean isHealthy(String serviceName, String regionName) {
        return circuitBreaker.isCallPermitted(regionKey(serviceName, regionName));
    }

    public Set<String> getUnhealthyRegions() {
        return unhealthyRegions;
    }

    public static String regionKey(String serviceName, String regionName) {
        return serviceName + ":" + regionName;
    }
}
