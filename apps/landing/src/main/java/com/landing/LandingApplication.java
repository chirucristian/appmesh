package com.landing;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

/**
 * AppMesh public landing service. Served at the apex {@code appmesh.org/} (routed
 * there by the gateway), it renders a live directory of every service currently in
 * the gateway's routing table and links each to {@code appmesh.org/<service>}.
 */
@SpringBootApplication
public class LandingApplication {
    public static void main(String[] args) {
        SpringApplication.run(LandingApplication.class, args);
    }
}
