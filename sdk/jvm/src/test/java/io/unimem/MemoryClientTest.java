package io.unimem;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
import java.util.List;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.concurrent.LinkedBlockingQueue;

class MemoryClientTest {
    @Test void javaAndKotlinRecordWrappers() throws Exception {
        var json = new ObjectMapper();
        var requests = new LinkedBlockingQueue<com.fasterxml.jackson.databind.JsonNode>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/memory/", exchange -> {
            var request = (com.fasterxml.jackson.databind.node.ObjectNode) json.readTree(exchange.getRequestBody());
            request.put("path", exchange.getRequestURI().getPath());
            requests.add(request);
            byte[] reply = json.writeValueAsBytes(json.createObjectNode().set("request", request));
            exchange.sendResponseHeaders(200, reply.length);
            exchange.getResponseBody().write(reply);
            exchange.close();
        });
        server.start();
        try {
            String url = "http://127.0.0.1:" + server.getAddress().getPort();
            var java = new MemoryClient(url, "secret", "alice", List.of("user:u"));
            var kotlin = new KotlinMemoryClient(url, "secret", "alice", List.of("user:u"));
            var record = json.createObjectNode().put("id", "fact-1").put("version", 2).put("kind", "fact");
            record.putObject("payload").put("text", "temporary fact").put("forget_after", "2030-01-01T00:00:00Z").put("forget_reason", "retention");
            var options = json.createObjectNode().put("event", "SessionEnd").put("budget", 0).put("session_id", "spoof");
            options.putObject("payload").put("succeeded", true);
            for (boolean useKotlin : List.of(false, true)) {
                var put = useKotlin ? kotlin.putRecord(record) : java.putRecord(record);
                assertEquals(record, put.get("request").get("record"));
                assertEquals("/v1/memory/record-put", requests.poll().get("path").asText());
                var search = useKotlin ? kotlin.searchRecords("temporary", 3) : java.searchRecords("temporary", 3);
                assertEquals(3, search.get("request").get("limit").asInt());
                assertEquals("temporary", requests.poll().get("query").asText());
                var forget = useKotlin ? kotlin.forgetRecord("fact-1", "retention") : java.forgetRecord("fact-1", "retention");
                assertEquals("retention", forget.get("request").get("reason").asText());
                assertEquals("fact-1", requests.poll().get("id").asText());
                var reap = useKotlin ? kotlin.reapDueRecords(16) : java.reapDueRecords();
                assertEquals("/v1/memory/record-reap-due", reap.get("request").get("path").asText());
                assertEquals(16, requests.poll().get("limit").asInt());
                var turn = useKotlin ? kotlin.turn("session", "query", options) : java.turn("session", "query", options);
                assertEquals("session", turn.get("request").get("session_id").asText());
                assertEquals("query", turn.get("request").get("query").asText());
                assertEquals("SessionEnd", turn.get("request").get("event").asText());
                assertTrue(turn.get("request").get("payload").get("succeeded").asBoolean());
                assertEquals(0, turn.get("request").get("budget").asInt());
                assertEquals(json.valueToTree(List.of("user:u", "agent:alice")), requests.poll().get("context"));
            }
            assertEquals("spoof", options.get("session_id").asText());
            for (int limit : List.of(-1, 0, 101)) {
                assertThrows(IllegalArgumentException.class, () -> java.reapDueRecords(limit));
                assertThrows(IllegalArgumentException.class, () -> kotlin.reapDueRecords(limit));
            }
            assertTrue(requests.isEmpty());
        } finally { server.stop(0); }
    }
    @Test void recordOperationsUseScopedWireContract() throws Exception {
        var json = new ObjectMapper();
        var requests = new LinkedBlockingQueue<com.fasterxml.jackson.databind.JsonNode>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/memory/", exchange -> {
            var request = (com.fasterxml.jackson.databind.node.ObjectNode) json.readTree(exchange.getRequestBody());
            request.put("path", exchange.getRequestURI().getPath());
            request.put("method", exchange.getRequestMethod());
            request.put("authorization", exchange.getRequestHeaders().getFirst("Authorization"));
            requests.add(request);
            byte[] reply = "{\"results\":[{\"deleted\":[\"fact-1\"]}],\"context\":\"memory\"}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, reply.length);
            exchange.getResponseBody().write(reply);
            exchange.close();
        });
        server.start();
        try {
            var client = new MemoryClient("http://127.0.0.1:" + server.getAddress().getPort(), "secret", "alice", List.of("user:u"));
            for (String operation : List.of("turn", "record-put", "record-search", "record-forget", "record-reap-due")) {
                String shape = switch (operation) {
                    case "turn" -> "{\"session_id\":\"session\",\"query\":\"hello\"}";
                    case "record-put" -> "{\"record\":{\"id\":\"fact-1\",\"kind\":\"fact\",\"version\":1,\"payload\":{\"text\":\"hello\"}}}";
                    case "record-search" -> "{\"query\":\"hello\",\"limit\":20}";
                    case "record-forget" -> "{\"id\":\"fact-1\",\"reason\":\"retention\"}";
                    default -> "{\"limit\":16}";
                };
                var arguments = (com.fasterxml.jackson.databind.node.ObjectNode) json.readTree(shape);
                arguments.putArray("context").add("agent:mallory");
                assertEquals("memory", client.call(operation, arguments).get("context").asText());
                var request = requests.poll(2, java.util.concurrent.TimeUnit.SECONDS);
                assertNotNull(request);
                assertEquals("/v1/memory/" + operation, request.get("path").asText());
                assertEquals("POST", request.get("method").asText());
                assertEquals("Bearer secret", request.get("authorization").asText());
                assertEquals(json.valueToTree(List.of("user:u", "agent:alice")), request.get("context"));
                assertEquals("agent:mallory", arguments.get("context").get(0).asText());
            }
        } finally { server.stop(0); }
    }
    @Test void scopedRequestAndRedirect() throws Exception {
        assertThrows(IllegalArgumentException.class, () -> new MemoryClient("http://remote.example", "", "", List.of()));
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/memory/add", exchange -> {
            var body = new ObjectMapper().readTree(exchange.getRequestBody());
            assertEquals("agent:alice", body.get("context").get(0).asText());
            byte[] reply = "{\"facts\":[]}".getBytes(java.nio.charset.StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, reply.length);
            exchange.getResponseBody().write(reply);
            exchange.close();
        });
        server.start();
        try {
            var client = new MemoryClient("http://127.0.0.1:" + server.getAddress().getPort(), "secret", "alice", List.of());
            assertTrue(client.add("explicit fact").get("facts").isArray());
        } finally { server.stop(0); }
    }
}
