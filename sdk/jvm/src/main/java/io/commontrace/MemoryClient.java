package io.commontrace;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;

/** The scoped memory API served by both the local binary and HTTP gateway. */
public final class MemoryClient {
    private final URI base;
    private final String token;
    private final List<String> scopes;
    private final HttpClient http;
    private final ObjectMapper json = new ObjectMapper();
    private static final Set<String> OPERATIONS = Set.of("add", "batch", "search", "profile", "reflect", "outcome", "check-action", "propose");
    public MemoryClient(String url, String token, String agent, List<String> context) {
        base = URI.create(url.replaceAll("/$", ""));
        boolean local = Set.of("localhost", "127.0.0.1", "[::1]", "::1").contains(base.getHost() == null ? "" : base.getHost());
        if (base.getUserInfo() != null || base.getQuery() != null || base.getFragment() != null ||
            !("https".equals(base.getScheme()) || ("http".equals(base.getScheme()) && local))) {
            throw new IllegalArgumentException("Use HTTPS or loopback without embedded credentials");
        }
        this.token = token;
        scopes = new ArrayList<>(context);
        if (!agent.isEmpty()) scopes.add("agent:" + agent);
        http = HttpClient.newBuilder().followRedirects(HttpClient.Redirect.NEVER).connectTimeout(Duration.ofSeconds(30)).build();
    }
    public JsonNode call(String operation, ObjectNode arguments) throws IOException, InterruptedException {
        if (!OPERATIONS.contains(operation)) throw new IllegalArgumentException("Unknown memory operation");
        ObjectNode body = arguments.deepCopy();
        body.set("context", json.valueToTree(scopes));
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create(base + "/v1/memory/" + operation))
            .timeout(Duration.ofSeconds(30)).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(body)));
        if (token != null && !token.isEmpty()) request.header("Authorization", "Bearer " + token);
        HttpResponse<java.io.InputStream> response = http.send(request.build(), HttpResponse.BodyHandlers.ofInputStream());
        try (var input = response.body()) {
            if (response.statusCode() < 200 || response.statusCode() >= 300) throw new IOException("Gateway HTTP " + response.statusCode());
            byte[] raw = input.readNBytes(8 * 1024 * 1024 + 1);
            if (raw.length > 8 * 1024 * 1024) throw new IOException("Response too large");
            JsonNode result = json.readTree(raw);
            if (result == null || !result.isObject()) throw new IOException("Response must be a JSON object");
            return result;
        }
    }
    public JsonNode add(String text) throws IOException, InterruptedException {
        return call("add", json.createObjectNode().put("text", text).put("local", true));
    }
    public JsonNode search(String query) throws IOException, InterruptedException {
        return call("search", json.createObjectNode().put("query", query));
    }
    public JsonNode profile(String query) throws IOException, InterruptedException {
        return call("profile", json.createObjectNode().put("query", query));
    }
    public JsonNode reflect(String query, String occasion, int budget) throws IOException, InterruptedException {
        return call("reflect", json.createObjectNode().put("query", query).put("occasion_id", occasion).put("budget", budget));
    }
    public JsonNode outcome(String occasion, boolean succeeded) throws IOException, InterruptedException {
        return call("outcome", json.createObjectNode().put("occasion_id", occasion).put("succeeded", succeeded));
    }
}
