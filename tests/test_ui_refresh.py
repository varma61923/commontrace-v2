"""Exercise the real console poll controller with deterministic asynchronous IO."""
import json
import pathlib
import shutil
import subprocess

import pytest

NODE = shutil.which("node")
SOURCE = pathlib.Path(__file__).resolve().parents[1] / "commontrace" / "ui" / "app.js"
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is needed to execute the frontend controller")


def run(script):
    source = SOURCE.read_text(encoding="utf-8")
    controller = source[source.index("  var refreshGeneration ="):source.index("  function schedule()")]
    harness = """
let token = 'first-token', location = {hash: '#/overview'}, route = 'overview';
let state = {}, lastOk = 0, painted = 0, connection = '', requested = [], pending = [];
let selected = {}, notice = null, lastPaint = '';
const main = {textContent: '', appendChild() {}};
const sessionStorage = {removeItem() {}};
function currentRoute() {return {id: route};}
function hashQuery() {return 'lesson_one';}
function viewAuth() {return {};}
function setConn(kind, text) {connection = kind;}
function ago() {return 'now';}
function paintHeader() {}
function render() {painted++;}
function paint(force) {painted++;}
function api(path) {requested.push(path); return Promise.resolve({path});}
"""
    output = subprocess.run([NODE, "-e", harness + controller + "\n(async()=>{" + script +
                             "})().catch(e=>{console.error(e);process.exit(1);});"],
                            capture_output=True, text=True, timeout=10, check=True)
    return json.loads(output.stdout)


@pytest.mark.parametrize(("route", "paths"), [
    ("overview", ["status", "memories", "agents"]),
    ("memories", ["status", "memories"]),
    ("live", ["status", "occasions"]),
    ("fleet", ["status", "agents"]),
    ("safety", ["status", "agents"]),
    ("review", ["status", "lessons?status=review"]),
    ("lesson", ["status", "memories", "lesson?slug=lesson_one"]),
    ("commands", ["status", "command-catalog"]),
])
def test_each_page_polls_its_sources_and_capabilities_have_a_freshness_window(route, paths):
    result = run(f"route={json.dumps(route)}; await refresh(); const first=requested.slice(); "
                 "requested=[]; await refresh(); const warm=requested.slice(); "
                 "capabilitiesAt=Date.now()-60001; requested=[]; await refresh(); "
                 "console.log(JSON.stringify({first,warm,expired:requested}));")
    assert result["first"] == ["/v1/status", "/v1/capabilities"] + ["/v1/" + p for p in paths[1:]]
    assert result["warm"] == ["/v1/" + p for p in paths]
    assert result["expired"] == result["first"]


def test_older_poll_cannot_overwrite_a_newer_page():
    result = run("""
api = function(path) {return new Promise((resolve,reject)=>pending.push({path,resolve,reject}));};
const older = refresh(); const oldRequests=pending.splice(0);
route='review'; location.hash='#/review'; const newer=refresh(); const newRequests=pending.splice(0);
newRequests.forEach(p=>p.resolve({version:'new'})); await newer;
oldRequests.forEach(p=>p.resolve({version:'old'})); await older;
console.log(JSON.stringify({status:state.status.version,painted,connection,agents:state.agents}));
""")
    assert result == {"status": "new", "painted": 1, "connection": "ok"}


def test_an_old_authentication_failure_cannot_clear_the_new_credential():
    result = run("""
api = function(path) {return new Promise((resolve,reject)=>pending.push({path,resolve,reject}));};
const older=refresh(); const oldRequests=pending.splice(0);
token='new-token'; const newer=refresh(); const newRequests=pending.splice(0);
newRequests.forEach(p=>p.resolve({version:'new'})); await newer;
const error=new Error('auth'); error.auth=true;
oldRequests.forEach(p=>p.reject(error)); await older;
console.log(JSON.stringify({token,connection,painted,status:state.status.version}));
""")
    assert result == {"token": "new-token", "connection": "ok", "painted": 2, "status": "new"}


def test_replacing_a_warm_credential_clears_retained_state_and_refetches_capabilities():
    result = run("""
await refresh();
state.lesson={version:'old'}; state.events={version:'old'};
state.commandCatalog={version:'old'}; state.commandResult={version:'old'};
selected={old:true}; notice={text:'old'}; lastPaint='old-paint';
api = function(path) {requested.push(path); return new Promise(resolve=>pending.push({path,resolve}));};
requested=[]; token='new-token'; route='review'; location.hash='#/review';
const newer=refresh();
const waiting={cleared:Object.values(state).every(value=>value===null),
              selected:Object.keys(selected),notice,lastPaint,lastOk};
pending.forEach(p=>p.resolve({version:'new'})); await newer;
console.log(JSON.stringify({waiting,requested,capabilities:state.capabilities.version,
                           capabilitiesToken,stateToken,agents:state.agents,
                           memories:state.memories,lesson:state.lesson,events:state.events,
                           commandCatalog:state.commandCatalog,commandResult:state.commandResult}));
""")
    assert result == {
        "waiting": {"cleared": True, "selected": [], "notice": None, "lastPaint": "", "lastOk": 0},
        "requested": ["/v1/status", "/v1/capabilities", "/v1/lessons?status=review"],
        "capabilities": "new", "capabilitiesToken": "new-token", "stateToken": "new-token",
        "agents": None, "memories": None, "lesson": None, "events": None,
        "commandCatalog": None, "commandResult": None,
    }


def test_a_current_authentication_failure_erases_retained_credential_state():
    result = run("""
await refresh(); selected={old:true}; notice={text:'old'}; lastPaint='old-paint';
api = function() {const error=new Error('auth');error.auth=true;return Promise.reject(error);};
await refresh();
console.log(JSON.stringify({token,cleared:Object.values(state).every(value=>value===null),
                           selected:Object.keys(selected),notice,lastPaint,lastOk,capabilitiesToken}));
""")
    assert result == {"token": "", "cleared": True, "selected": [], "notice": None,
                      "lastPaint": "", "lastOk": 0, "capabilitiesToken": ""}
