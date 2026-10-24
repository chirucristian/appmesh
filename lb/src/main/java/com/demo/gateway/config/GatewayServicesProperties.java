package com.demo.gateway.config;

import org.springframework.boot.context.properties.ConfigurationProperties;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Configuration properties for all gateway-managed services and their regions.
 * Bound from the "gateway" prefix in application.yml.
 */
@ConfigurationProperties(prefix = "gateway")
public class GatewayServicesProperties {

    /**
     * Map of service-name → service configuration.
     */
    private Map<String, ServiceConfig> services = new LinkedHashMap<>();

    private CircuitBreakerConfig circuitBreaker = new CircuitBreakerConfig();
    private HealthCheckConfig healthCheck = new HealthCheckConfig();
    private TimeoutConfig timeout = new TimeoutConfig();

    // ── getters / setters ──────────────────────────────────────────────

    public Map<String, ServiceConfig> getServices() { return services; }
    public void setServices(Map<String, ServiceConfig> services) { this.services = services; }

    public CircuitBreakerConfig getCircuitBreaker() { return circuitBreaker; }
    public void setCircuitBreaker(CircuitBreakerConfig circuitBreaker) { this.circuitBreaker = circuitBreaker; }

    public HealthCheckConfig getHealthCheck() { return healthCheck; }
    public void setHealthCheck(HealthCheckConfig healthCheck) { this.healthCheck = healthCheck; }

    public TimeoutConfig getTimeout() { return timeout; }
    public void setTimeout(TimeoutConfig timeout) { this.timeout = timeout; }

    // ── nested classes ─────────────────────────────────────────────────

    public static class ServiceConfig {
        /** Path patterns this service matches (e.g. /police-service/**) */
        private List<String> paths = new ArrayList<>();
        /** Health-check path on the upstream service (e.g. /health, /status) */
        private String healthCheckPath = "/health";
        /** Ordered list of regions – first healthy one is preferred */
        private List<RegionConfig> regions = new ArrayList<>();

        public List<String> getPaths() { return paths; }
        public void setPaths(List<String> paths) { this.paths = paths; }

        public String getHealthCheckPath() { return healthCheckPath; }
        public void setHealthCheckPath(String healthCheckPath) { this.healthCheckPath = healthCheckPath; }

        public List<RegionConfig> getRegions() { return regions; }
        public void setRegions(List<RegionConfig> regions) { this.regions = regions; }
    }

    public static class RegionConfig {
        private String name;
        private String url;

        public String getName() { return name; }
        public void setName(String name) { this.name = name; }

        public String getUrl() { return url; }
        public void setUrl(String url) { this.url = url; }
    }

    public static class CircuitBreakerConfig {
        /** Number of consecutive failures to open the circuit */
        private int failureThreshold = 3;
        /** Number of consecutive successes in HALF_OPEN to close the circuit */
        private int successThreshold = 2;
        /** Seconds to wait before transitioning OPEN → HALF_OPEN */
        private int halfOpenDelaySeconds = 30;

        public int getFailureThreshold() { return failureThreshold; }
        public void setFailureThreshold(int failureThreshold) { this.failureThreshold = failureThreshold; }

        public int getSuccessThreshold() { return successThreshold; }
        public void setSuccessThreshold(int successThreshold) { this.successThreshold = successThreshold; }

        public int getHalfOpenDelaySeconds() { return halfOpenDelaySeconds; }
        public void setHalfOpenDelaySeconds(int halfOpenDelaySeconds) { this.halfOpenDelaySeconds = halfOpenDelaySeconds; }
    }

    public static class HealthCheckConfig {
        /** Interval in seconds between periodic health checks */
        private int intervalSeconds = 15;
        /** Timeout in seconds for a single health-check request */
        private int timeoutSeconds = 5;

        public int getIntervalSeconds() { return intervalSeconds; }
        public void setIntervalSeconds(int intervalSeconds) { this.intervalSeconds = intervalSeconds; }

        public int getTimeoutSeconds() { return timeoutSeconds; }
        public void setTimeoutSeconds(int timeoutSeconds) { this.timeoutSeconds = timeoutSeconds; }
    }

    public static class TimeoutConfig {
        /** Connect timeout in milliseconds for proxied requests */
        private int connectTimeoutMs = 2000;
        /** Read (response) timeout in milliseconds for proxied requests */
        private int readTimeoutMs = 5000;

        public int getConnectTimeoutMs() { return connectTimeoutMs; }
        public void setConnectTimeoutMs(int connectTimeoutMs) { this.connectTimeoutMs = connectTimeoutMs; }

        public int getReadTimeoutMs() { return readTimeoutMs; }
        public void setReadTimeoutMs(int readTimeoutMs) { this.readTimeoutMs = readTimeoutMs; }
    }
}
