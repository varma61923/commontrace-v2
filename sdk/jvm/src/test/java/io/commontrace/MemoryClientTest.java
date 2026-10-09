package io.commontrace;
import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;
import java.util.List;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;

class MemoryClientTest {
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
