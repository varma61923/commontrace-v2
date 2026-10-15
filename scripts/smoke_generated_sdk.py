"""Exercise an upstream-generated TypeScript client against the real HTTP gateway."""
from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import tempfile
import threading
from pathlib import Path

from commontrace import holdout_io, memory_control
from commontrace.gateway import Gateway, make_http_server

SMOKE = r'''
const assert = require('node:assert/strict');
const {MemoryApi, ConversationsApi, HistoryApi, Configuration, ResponseError} =
    require(process.env.COMMONTRACE_GENERATED_SDK);
const configuration = new Configuration({basePath: process.env.COMMONTRACE_SMOKE_URL,
    accessToken: process.env.COMMONTRACE_SMOKE_TOKEN});
const api = new MemoryApi(configuration);
(async () => {
    const added = await api.memoryAdd({addRequest: {text: 'Office is Tokyo', context: ['user:smoke']}});
    assert.equal(added.facts.length, 1);
    const batch = await api.memoryBatch({batchRequest: {
        items: [{statement: 'Office opens at nine'}], context: ['user:smoke']}});
    assert.equal(batch.facts.length, 1);
    const search = await api.memorySearch({searchRequest: {query: 'office', context: ['user:smoke']}});
    assert.equal(search.results.length, 2);
    const snapshot = new Date().toISOString();
    const temporal = await api.memorySearch({searchRequest: {query: 'office',
        context: ['user:smoke'], asOf: snapshot, knownAt: snapshot, graphHops: 0, reranker: 'mmr'}});
    assert.equal(temporal.results.length, 2);
    await assert.rejects(api.memorySearch({searchRequest: {query: 'office', graphHops: 5}}),
        error => error instanceof ResponseError && error.response.status === 400);
    assert.equal((await api.memoryCheckAction({
        checkActionRequest: {tool: 'read', context: ['user:smoke']}})).allowed, true);
    const profile = await api.memoryProfile({
        profileRequest: {query: 'office', context: ['user:smoke'], occasionId: 'sdk-profile'}});
    assert.equal(profile.dynamic.length, 2);
    assert.equal(profile.occasionId, 'sdk-profile');
    assert.ok(profile.recentActivity.some(row => row.activityType === 'action_check'));
    const reflected = await api.memoryReflect({
        reflectRequest: {query: 'office', context: ['user:smoke'], occasionId: 'sdk-reflect', budget: 200}});
    assert.ok(reflected.context.includes('Tokyo'));
    assert.equal(reflected.occasionId, 'sdk-reflect');
    assert.ok(reflected.tokensEstimate <= reflected.budget);
    assert.ok(Array.isArray(reflected.withheld));
    assert.equal(typeof reflected.withdrawn, 'object');
    const outcome = await api.memoryOutcome({outcomeRequest: {occasionId: reflected.occasionId, succeeded: true}});
    assert.equal(typeof outcome.recorded, 'boolean');
    const proposed = await api.memoryPropose({proposeRequest: {text: 'Review deployments',
        sources: [added.facts[0].id], context: ['user:smoke']}});
    assert.equal(proposed.kind, 'proposal');
    await assert.rejects(api.memoryCheckAction({checkActionRequest: {tool: 'deploy', context: ['user:smoke']}}),
        error => error instanceof ResponseError && error.response.status === 403);
    console.log('Generated TypeScript SDK: eight operations and directive rejection passed');
    if (ConversationsApi) {
        const conversations = new ConversationsApi(configuration);
        const saved = await conversations.conversationAdd({conversationAddRequest: {
            space: 'sdk-conversation', session: 'work', sessionAt: '2025-01-10',
            messages: [{speaker: 'user', text: 'The Orion project uses PostgreSQL for its invoices.'},
                       {speaker: 'user', text: 'PostgreSQL invoices are backed up in Oslo.'}]}});
        assert.equal(saved.added, 2);
        const recalled = await conversations.conversationRecall({conversationRecallRequest: {
            space: 'sdk-conversation', question: 'Where are Orion project invoices backed up?',
            embedder: 'none', rerank: 'mmr', graphHops: 2, secondPass: true,
            entityBoost: 0.1, lexicalWeight: 0.5, summaries: false, budget: 1000}});
        assert.ok(recalled.context.includes('Oslo'));
        assert.ok(recalled.context.includes('Orion'));
        assert.ok(recalled.tokens <= 1000);
        assert.equal(recalled.explain.rerank_stage.reranker, 'mmr');
        const dated = await conversations.conversationRecall({conversationRecallRequest: {
            space: 'sdk-conversation', question: 'What happened in January 2025?',
            now: '2025-02-01', embedder: 'none', rerank: null, graphHops: 0, budget: 1000}});
        assert.equal(dated.window.length, 3);
        const summary = await conversations.conversationSummarize({conversationSummarizeRequest: {
            space: 'sdk-conversation', session: 'work', mode: 'extractive'}});
        assert.equal(summary.turns, 2);
        assert.ok(summary.text.includes('Orion'));
        const sessions = await conversations.conversationSessions({space: 'sdk-conversation'});
        assert.equal(sessions.sessions.length, 1);
        const history = new HistoryApi(configuration);
        assert.ok(Array.isArray((await history.traceHistory()).traces));
        assert.ok(Array.isArray((await history.benchmarkHistory()).reports));
        await assert.rejects(conversations.conversationRecall({conversationRecallRequest: {
            space: 'sdk-conversation', question: 'Orion', graphHops: 3}}),
            error => error instanceof ResponseError && error.response.status === 400);
        console.log('Generated gateway SDK: conversation, temporal, summary and history workflows passed');
    }
})().catch(error => { console.error(error.name, error.response?.status); process.exitCode = 1; });
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sdk', type=Path, help='generated TypeScript directory after npm run build')
    args = parser.parse_args()
    module = args.sdk.resolve()/'dist'/'index.js'
    if not module.is_file():
        parser.error('compile the generated client before the smoke test')
    with tempfile.TemporaryDirectory(prefix='commontrace-generated-sdk-') as root:
        holdout_io.configure(root, rate=0, salt='generated-sdk-smoke')
        memory_control.directive(root, 'Deployment prohibited', deny_tools=['deploy'])
        token = secrets.token_urlsafe(32)
        server = make_http_server(Gateway(root, token=token), '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            environment = {**os.environ, 'COMMONTRACE_GENERATED_SDK': str(module),
                           'COMMONTRACE_SMOKE_URL': 'http://127.0.0.1:'+str(server.server_port),
                           'COMMONTRACE_SMOKE_TOKEN': token}
            subprocess.run(['node', '-e', SMOKE], env=environment, check=True, timeout=60)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == '__main__':
    main()
