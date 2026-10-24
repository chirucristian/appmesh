package com.demo.gateway.health;

import com.demo.gateway.config.GatewayServicesProperties;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

import java.time.Duration;
import java.time.Instant;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

/**
 * Simple circuit breaker registry keyed by "serviceName:regionName".
 * <p>
 * States:
 * <ul>
 *   <li>CLOSED  – traffic flows normally</li>
 *   <li>OPEN    – region is considered down; calls are skipped</li>
 *   <li>HALF_OPEN – after a cool-down period a few probe calls are allowed through</li>
 * </ul>
 */
@Component
public class CircuitBreakerRegistry {

    private static final Logger log = LoggerFactory.getLogger(CircuitBreakerRegistry.class);

    public enum State { CLOSED, OPEN, HALF_OPEN }

    // ── per-key state ──────────────────────────────────────────────────

    static class CBState {
        volatile State state = State.CLOSED;
        int failureCount = 0;
        int successCount = 0;
        Instant lastFailureTime = Instant.EPOCH;
    }

    private final ConcurrentHashMap<String, CBState> states = new ConcurrentHashMap<>();

    private final int failureThreshold;
    private final int successThreshold;
    private final Duration halfOpenDelay;

    public CircuitBreakerRegistry(GatewayServicesProperties props) {
        GatewayServicesProperties.CircuitBreakerConfig cb = props.getCircuitBreaker();
        this.failureThreshold = cb.getFailureThreshold();
        this.successThreshold = cb.getSuccessThreshold();
        this.halfOpenDelay = Duration.ofSeconds(cb.getHalfOpenDelaySeconds());
    }

    // ── public API ─────────────────────────────────────────────────────

    /** Returns true if a call to this region is permitted. */
    public boolean isCallPermitted(String key) {
        CBState s = states.computeIfAbsent(key, k -> new CBState());
        synchronized (s) {
            switch (s.state) {
                case CLOSED:
                    return true;
                case OPEN:
                    if (Duration.between(s.lastFailureTime, Instant.now()).compareTo(halfOpenDelay) > 0) {
                        log.info("Circuit breaker {} transitioning OPEN → HALF_OPEN", key);
                        s.state = State.HALF_OPEN;
                        s.successCount = 0;
                        return true;
                    }
                    return false;
                case HALF_OPEN:
                    return true;
                default:
                    return true;
            }
        }
    }

    /** Record a successful call – may close the circuit. */
    public void recordSuccess(String key) {
        CBState s = states.computeIfAbsent(key, k -> new CBState());
        synchronized (s) {
            s.failureCount = 0;
            if (s.state == State.HALF_OPEN) {
                s.successCount++;
                if (s.successCount >= successThreshold) {
                    log.info("Circuit breaker {} transitioning HALF_OPEN → CLOSED", key);
                    s.state = State.CLOSED;
                    s.successCount = 0;
                }
            }
        }
    }

    /** Record a failed call – may open the circuit. */
    public void recordFailure(String key) {
        CBState s = states.computeIfAbsent(key, k -> new CBState());
        synchronized (s) {
            s.lastFailureTime = Instant.now();
            s.successCount = 0;
            s.failureCount++;

            if (s.state == State.HALF_OPEN) {
                log.warn("Circuit breaker {} transitioning HALF_OPEN → OPEN (failure during probe)", key);
                s.state = State.OPEN;
                s.failureCount = 0;
            } else if (s.state == State.CLOSED && s.failureCount >= failureThreshold) {
                log.warn("Circuit breaker {} transitioning CLOSED → OPEN ({} consecutive failures)",
                        key, s.failureCount);
                s.state = State.OPEN;
                s.failureCount = 0;
            }
        }
    }

    /** Retrieve current state for a key (for status reporting). */
    public State getState(String key) {
        CBState s = states.get(key);
        if (s == null) return State.CLOSED;
        // trigger potential OPEN→HALF_OPEN check
        isCallPermitted(key);
        return s.state;
    }

    /** Returns a snapshot of all tracked keys and their states. */
    public Map<String, State> snapshot() {
        Map<String, State> result = new ConcurrentHashMap<>();
        states.forEach((k, v) -> {
            isCallPermitted(k); // trigger potential state transition
            result.put(k, v.state);
        });
        return result;
    }
}
