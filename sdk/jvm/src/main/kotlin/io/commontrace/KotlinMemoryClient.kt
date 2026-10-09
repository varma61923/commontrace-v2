package io.commontrace

import com.fasterxml.jackson.databind.JsonNode
import com.fasterxml.jackson.databind.node.ObjectNode

/** Kotlin facade; the same pinned context, redirect policy and JSON contracts. */
class KotlinMemoryClient(url: String, token: String = "", agent: String = "", context: List<String> = emptyList()) {
    private val memory = MemoryClient(url, token, agent, context)
    fun add(text: String): JsonNode = memory.add(text)
    fun search(query: String): JsonNode = memory.search(query)
    fun profile(query: String = ""): JsonNode = memory.profile(query)
    fun reflect(query: String, occasion: String, budget: Int = 600): JsonNode = memory.reflect(query, occasion, budget)
    fun outcome(occasion: String, succeeded: Boolean): JsonNode = memory.outcome(occasion, succeeded)
    fun call(operation: String, arguments: ObjectNode): JsonNode = memory.call(operation, arguments)
}
