package com.police.controller;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

import java.util.Map;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.stereotype.Controller;
import org.springframework.ui.Model;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.ResponseBody;

import jakarta.annotation.PostConstruct;

@Controller
public class HomeController {

    @Value("${app.version}")
    private String version;

    private String instanceId;
    private String region;

    @PostConstruct
    public void init() {
        String token = fetchMetadataToken();
        this.instanceId = fetchMetadata(token, "instance-id");
        this.region = fetchRegion(token);
    }

    @GetMapping("/")
    public String home(Model model) {
        model.addAttribute("version", version);
        model.addAttribute("instanceId", instanceId);
        model.addAttribute("region", region);
        return "index";
    }

    @GetMapping("/status")
    @ResponseBody
    public ResponseEntity<Map<String, String>> status() {
        return ResponseEntity.ok(Map.of(
                "status", "UP",
                "version", version,
                "instanceId", instanceId,
                "region", region
        ));
    }

    /**
     * AppMesh welcome page. Linked from the gateway landing page
     * (appmesh.org → /police-service/welcome). Surfaces the serving region and
     * instance so multi-region failover is visible to the user.
     */
    @GetMapping(value = "/welcome", produces = "text/html; charset=utf-8")
    @ResponseBody
    public String welcome() {
        return WELCOME_HTML
                .replace("{NAME}", "Police Service")
                .replace("{ACCENT}", "#4f8cff")
                .replace("{EMOJI}", "🚔")
                .replace("{REGION}", region == null ? "N/A" : region)
                .replace("{INSTANCE}", instanceId == null ? "N/A" : instanceId)
                .replace("{VERSION}", version);
    }

    private static final String WELCOME_HTML = """
            <!DOCTYPE html>
            <html lang="en"><head>
            <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>{NAME} — AppMesh</title>
            <style>
            :root{--accent:{ACCENT}}
            *{margin:0;padding:0;box-sizing:border-box}
            body{min-height:100vh;display:flex;align-items:center;justify-content:center;
             font-family:'Segoe UI',system-ui,sans-serif;
             background:radial-gradient(ellipse at top,#1a2238,#0a0e1a);color:#e6e9f0;padding:24px}
            .card{width:100%;max-width:560px;background:rgba(255,255,255,0.04);
             border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:48px;
             box-shadow:0 24px 60px rgba(0,0,0,0.4);text-align:center}
            .badge{display:inline-block;font-size:12px;letter-spacing:2px;text-transform:uppercase;
             color:var(--accent);border:1px solid var(--accent);border-radius:999px;padding:5px 14px;margin-bottom:24px}
            .emoji{font-size:64px;line-height:1;margin-bottom:16px}
            h1{font-size:32px;margin-bottom:8px}
            .sub{color:#9aa3b2;margin-bottom:32px}
            .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;text-align:left;margin-bottom:32px}
            .fact{background:rgba(0,0,0,0.25);border-radius:12px;padding:14px 16px}
            .fact .k{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#6b7382}
            .fact .v{font-size:16px;font-weight:600;margin-top:4px;word-break:break-all}
            .links{display:flex;gap:12px;justify-content:center}
            a.btn{text-decoration:none;padding:12px 22px;border-radius:10px;font-weight:600;font-size:14px}
            a.primary{background:var(--accent);color:#0a0e1a}
            a.ghost{border:1px solid rgba(255,255,255,0.2);color:#e6e9f0}
            .foot{margin-top:28px;font-size:12px;color:#5a6270}
            </style></head><body>
            <div class="card">
             <span class="badge">AppMesh · routed</span>
             <div class="emoji">{EMOJI}</div>
             <h1>Welcome to {NAME}</h1>
             <p class="sub">You reached this service through the AppMesh gateway.</p>
             <div class="grid">
              <div class="fact"><div class="k">Region</div><div class="v">{REGION}</div></div>
              <div class="fact"><div class="k">Instance</div><div class="v">{INSTANCE}</div></div>
              <div class="fact"><div class="k">Version</div><div class="v">v{VERSION}</div></div>
              <div class="fact"><div class="k">Status</div><div class="v" style="color:var(--accent)">● UP</div></div>
             </div>
             <div class="links">
              <a class="btn primary" href="./">Enter the app →</a>
              <a class="btn ghost" href="../">← All services</a>
             </div>
             <p class="foot">Served by {NAME} · {REGION}</p>
            </div></body></html>
            """;

    private String fetchMetadataToken() {
        try {
            HttpClient client = HttpClient.newBuilder()
                    .connectTimeout(Duration.ofSeconds(2))
                    .build();

            HttpRequest tokenRequest = HttpRequest.newBuilder()
                    .uri(URI.create("http://169.254.169.254/latest/api/token"))
                    .header("X-aws-ec2-metadata-token-ttl-seconds", "21600")
                    .PUT(HttpRequest.BodyPublishers.noBody())
                    .timeout(Duration.ofSeconds(2))
                    .build();

            HttpResponse<String> tokenResponse = client.send(tokenRequest,
                    HttpResponse.BodyHandlers.ofString());
            return tokenResponse.body();
        } catch (Exception e) {
            return null;
        }
    }

    private String fetchMetadata(String token, String path) {
        if (token == null) return "N/A";
        try {
            HttpClient client = HttpClient.newBuilder()
                    .connectTimeout(Duration.ofSeconds(2))
                    .build();

            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create("http://169.254.169.254/latest/meta-data/" + path))
                    .header("X-aws-ec2-metadata-token", token)
                    .GET()
                    .timeout(Duration.ofSeconds(2))
                    .build();

            HttpResponse<String> response = client.send(request,
                    HttpResponse.BodyHandlers.ofString());

            if (response.statusCode() == 200) {
                return response.body().trim();
            }
        } catch (Exception e) {
            // Not running on AWS or metadata service unavailable
        }
        return "N/A";
    }

    private String fetchRegion(String token) {
        String az = fetchMetadata(token, "placement/availability-zone");
        if (!"N/A".equals(az) && az.length() > 1) {
            return az.substring(0, az.length() - 1);
        }
        return "N/A";
    }
}
