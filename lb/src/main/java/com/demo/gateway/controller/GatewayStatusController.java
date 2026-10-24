package com.demo.gateway.controller;

import com.demo.gateway.config.GatewayServicesProperties;
import com.demo.gateway.config.GatewayServicesProperties.RegionConfig;
import com.demo.gateway.config.GatewayServicesProperties.ServiceConfig;
import com.demo.gateway.health.CircuitBreakerRegistry;
import com.demo.gateway.health.RegionHealthMonitor;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import reactor.core.publisher.Mono;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Exposes the current health / circuit-breaker state of every service region
 * at {@code /gateway/status}.
 */
@RestController
public class GatewayStatusController {

    private final GatewayServicesProperties properties;
    private final CircuitBreakerRegistry circuitBreaker;
    private final RegionHealthMonitor healthMonitor;

    public GatewayStatusController(GatewayServicesProperties properties,
                                   CircuitBreakerRegistry circuitBreaker,
                                   RegionHealthMonitor healthMonitor) {
        this.properties = properties;
        this.circuitBreaker = circuitBreaker;
        this.healthMonitor = healthMonitor;
    }

    /** Liveness probe for the ALB target group — every AppMesh service answers on /status. */
    @GetMapping("/status")
    public Mono<Map<String, Object>> health() {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("status", "UP");
        body.put("service", "gateway");
        return Mono.just(body);
    }

    @GetMapping("/gateway/status")
    public Mono<Map<String, Object>> status() {
        Map<String, Object> result = new LinkedHashMap<>();

        for (Map.Entry<String, ServiceConfig> entry : properties.getServices().entrySet()) {
            String serviceName = entry.getKey();
            ServiceConfig svc = entry.getValue();

            Map<String, Object> serviceInfo = new LinkedHashMap<>();
            serviceInfo.put("paths", svc.getPaths());
            serviceInfo.put("healthCheckPath", svc.getHealthCheckPath());
            boolean multiRegion = svc.getRegions() != null && svc.getRegions().size() > 1;
            serviceInfo.put("mode", multiRegion ? "multi-region-failover" : "single-region");

            List<Map<String, Object>> regionInfos = new ArrayList<>();
            for (RegionConfig region : svc.getRegions()) {
                String key = RegionHealthMonitor.regionKey(serviceName, region.getName());
                Map<String, Object> ri = new LinkedHashMap<>();
                ri.put("name", region.getName());
                ri.put("url", region.getUrl());
                if (multiRegion) {
                    ri.put("circuitBreaker", circuitBreaker.getState(key).name());
                    ri.put("healthy", healthMonitor.isHealthy(serviceName, region.getName()));
                }
                regionInfos.add(ri);
            }
            serviceInfo.put("regions", regionInfos);
            result.put(serviceName, serviceInfo);
        }

        return Mono.just(result);
    }
}
