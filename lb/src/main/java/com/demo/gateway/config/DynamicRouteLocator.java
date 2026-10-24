package com.demo.gateway.config;

import com.demo.gateway.config.GatewayServicesProperties.ServiceConfig;
import com.demo.gateway.filter.MultiRegionRouteFilter;
import com.demo.gateway.health.CircuitBreakerRegistry;
import org.springframework.cloud.gateway.route.Route;
import org.springframework.cloud.gateway.route.RouteLocator;
import org.springframework.cloud.gateway.route.builder.RouteLocatorBuilder;
import org.springframework.stereotype.Component;
import reactor.core.publisher.Flux;

import java.util.List;

/**
 * Builds one proxy route per service from {@link GatewayServicesProperties},
 * attaching a {@link MultiRegionRouteFilter} that performs the actual multi-region
 * failover routing.
 * <p>
 * {@link #getRoutes()} reads the properties bean live, so when
 * {@link S3RouteConfigLoader} repopulates it and publishes a
 * {@code RefreshRoutesEvent}, Spring Cloud Gateway's caching route locator re-invokes
 * this method and the route table is rebuilt against the new topology.
 * <p>
 * The {@code no://op} URI is a deliberate placeholder: the filter calls
 * {@code setAlreadyRouted(exchange)}, so the default Netty routing filter never
 * consumes the URI — all proxying happens inside {@link MultiRegionRouteFilter}.
 */
@Component
public class DynamicRouteLocator implements RouteLocator {

    private final RouteLocatorBuilder builder;
    private final GatewayServicesProperties properties;
    private final CircuitBreakerRegistry circuitBreaker;

    public DynamicRouteLocator(RouteLocatorBuilder builder,
                               GatewayServicesProperties properties,
                               CircuitBreakerRegistry circuitBreaker) {
        this.builder = builder;
        this.properties = properties;
        this.circuitBreaker = circuitBreaker;
    }

    @Override
    public Flux<Route> getRoutes() {
        RouteLocatorBuilder.Builder routes = builder.routes();

        properties.getServices().forEach((serviceName, svc) -> {
            List<String> paths = svc.getPaths();
            if (paths == null || paths.isEmpty()) {
                return;
            }

            MultiRegionRouteFilter filter =
                    new MultiRegionRouteFilter(serviceName, properties, circuitBreaker);
            routes.route(serviceName, r -> r
                    .path(paths.toArray(new String[0]))
                    .filters(f -> f.filter(filter))
                    .uri("no://op"));
        });

        return routes.build().getRoutes();
    }
}
