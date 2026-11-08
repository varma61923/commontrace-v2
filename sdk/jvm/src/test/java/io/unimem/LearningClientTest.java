package io.unimem;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
import java.util.List;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.atomic.AtomicReference;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;

class LearningClientTest {
    @Test void javaAndKotlinLearningNegotiateAndPinWireFields() throws Exception {
        var json = new ObjectMapper();
        var schema = new AtomicReference<>("unimem.learning.v1");
        var requests = new LinkedBlockingQueue<JsonNode>();
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        server.createContext("/v1/learning/", exchange -> {
            assertEquals("Bearer key", exchange.getRequestHeaders().getFirst("Authorization"));
            if (exchange.getRequestMethod().equals("GET")) {
                assertTrue(exchange.getRequestURI().getRawQuery().startsWith("context=user%3Au&context=agent%3Aa"));
            } else {
                requests.add(json.readTree(exchange.getRequestBody()));
            }
            var reply = json.createObjectNode().put("schema", schema.get());
            reply.set("operations", json.valueToTree(List.of("configure", "assign", "delivery", "outcome", "run", "status", "rollback")));
            byte[] bytes = json.writeValueAsBytes(reply);
            exchange.sendResponseHeaders(200, bytes.length);
            exchange.getResponseBody().write(bytes);
            exchange.close();
        });
        server.start();
        try {
            String url = "http://127.0.0.1:" + server.getAddress().getPort();
            var java = new MemoryClient(url, "key", "a", List.of("user:u"));
            var kotlin = new KotlinMemoryClient(url, "key", "a", List.of("user:u"));
            for (var learning : List.of(java.learning, kotlin.getLearning())) {
                learning.configure("observe", json.createObjectNode().put("min_arm", 10));
                learning.assign("o", List.of("m"), json.createObjectNode().put("experiment_id", "e"));
                learning.delivery("o", List.of(), "e");
                learning.outcome("o", json.createObjectNode().put("reward", 0.8), "e");
                learning.run(json.createObjectNode().put("limit", 2));
                learning.status("e");
                learning.rollback("regression", json.createObjectNode());
                assertEquals("observe", requests.poll().get("mode").asText());
                assertEquals("e", requests.poll().get("experiment_id").asText());
                assertEquals(json.createArrayNode(), requests.poll().get("memory_ids"));
                var outcome = requests.poll();
                assertEquals(0.8, outcome.get("reward").asDouble());
                assertEquals("e", outcome.get("experiment_id").asText());
                assertEquals(json.valueToTree(List.of("user:u", "agent:a")), outcome.get("context"));
                requests.poll();requests.poll();
            }
            assertTrue(requests.isEmpty());
            schema.set("old");
            assertThrows(java.io.IOException.class, () -> java.learning.run(json.createObjectNode()));
            assertTrue(requests.isEmpty());
        } finally {server.stop(0);}
    }
}
