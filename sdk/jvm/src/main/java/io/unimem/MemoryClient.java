package io.unimem;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.io.IOException;
import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.net.http.HttpTimeoutException;
import java.nio.ByteBuffer;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionStage;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.Flow;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;

/** The scoped memory API served by both the local binary and HTTP gateway. */
public final class MemoryClient {
    private final URI base;
    private final String token;
    private final List<String> scopes;
    private final HttpClient http;
    private final Duration timeout;
    private final ObjectMapper json = new ObjectMapper();
    private static final int MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
    private static final Set<String> OPERATIONS = Set.of("add", "batch", "search", "profile", "reflect", "outcome", "check-action", "propose",
        "turn", "record-put", "record-search", "record-forget", "record-reap-due");
    public MemoryClient(String url, String token, String agent, List<String> context) {
        this(url, token, agent, context, Duration.ofSeconds(30));
    }
    /** Bound a complete request, including the response body, to at most 30 seconds. */
    public MemoryClient(String url, String token, String agent, List<String> context, Duration timeout) {
        if (timeout == null || timeout.isZero() || timeout.isNegative() || timeout.compareTo(Duration.ofSeconds(30)) > 0) {
            throw new IllegalArgumentException("Request timeout must be positive and at most 30 seconds");
        }
        this.timeout = timeout;
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
            .timeout(timeout).header("Content-Type", "application/json")
            .POST(HttpRequest.BodyPublishers.ofString(json.writeValueAsString(body)));
        if (token != null && !token.isEmpty()) request.header("Authorization", "Bearer " + token);
        // An InputStream body completes send() when only the headers arrive.
        // Await a bounded subscriber instead, and cancel the exchange on deadline/interruption.
        var pending = http.sendAsync(request.build(), info -> new BoundedBody(info.statusCode()));
        try {
            byte[] raw = pending.get(timeout.toNanos(), TimeUnit.NANOSECONDS).body();
            JsonNode result = json.readTree(raw);
            if (result == null || !result.isObject()) throw new IOException("Response must be a JSON object");
            return result;
        } catch (TimeoutException e) {
            pending.cancel(true);
            throw new HttpTimeoutException("Memory request exceeded its response deadline");
        } catch (InterruptedException e) {
            pending.cancel(true);
            throw e;
        } catch (ExecutionException e) {
            if (e.getCause() instanceof IOException cause) throw cause;
            throw new IOException("Memory request failed", e.getCause());
        }
    }

    private static final class BoundedBody implements HttpResponse.BodySubscriber<byte[]> {
        private final CompletableFuture<byte[]> result = new CompletableFuture<>();
        private final ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        private final int status;
        private Flow.Subscription subscription;

        BoundedBody(int status) { this.status = status; }
        public CompletionStage<byte[]> getBody() { return result; }
        public void onSubscribe(Flow.Subscription value) {
            subscription = value;
            if (status < 200 || status >= 300) {
                fail(new IOException("Gateway HTTP " + status));
            } else {
                value.request(1);
            }
        }
        public void onNext(List<ByteBuffer> buffers) {
            for (var buffer : buffers) {
                int size = buffer.remaining();
                if (size > MAX_RESPONSE_BYTES - bytes.size()) {
                    fail(new IOException("Response too large"));
                    return;
                }
                byte[] chunk = new byte[size];
                buffer.get(chunk);
                bytes.writeBytes(chunk);
            }
            subscription.request(1);
        }
        private void fail(IOException error) {
            subscription.cancel();
            result.completeExceptionally(error);
        }
        public void onError(Throwable error) { result.completeExceptionally(error); }
        public void onComplete() { result.complete(bytes.toByteArray()); }
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
    public JsonNode putRecord(ObjectNode record) throws IOException, InterruptedException {
        return call("record-put", json.createObjectNode().set("record", record));
    }
    public JsonNode searchRecords(String query, int limit) throws IOException, InterruptedException {
        return call("record-search", json.createObjectNode().put("query", query).put("limit", limit));
    }
    public JsonNode forgetRecord(String id, String reason) throws IOException, InterruptedException {
        return call("record-forget", json.createObjectNode().put("id", id).put("reason", reason));
    }
    /** Erase a bounded batch of due roots and their dependents; hosted use requires admin scope. */
    public JsonNode reapDueRecords(int limit) throws IOException, InterruptedException {
        if (limit < 1 || limit > 100) throw new IllegalArgumentException("Expiration batch must be in 1..100");
        return call("record-reap-due", json.createObjectNode().put("limit", limit));
    }
    public JsonNode reapDueRecords() throws IOException, InterruptedException {
        return reapDueRecords(16);
    }
    /** Capture and retrieve in one request; session, query and context remain pinned. */
    public JsonNode turn(String session, String query, ObjectNode options) throws IOException, InterruptedException {
        ObjectNode body = options.deepCopy();
        body.put("session_id", session).put("query", query);
        return call("turn", body);
    }
    public JsonNode turn(String session, String query) throws IOException, InterruptedException {
        return turn(session, query, json.createObjectNode());
    }
}
