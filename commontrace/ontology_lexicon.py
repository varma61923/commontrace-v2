"""Offline evidence for typing entity mentions: name lists, mention shapes,
hypernyms and context cues for the built-in entity types.

Everything here is data plus compiled patterns; `ontology_classify` turns it into
per-type scores. The lists are deliberately general (well-known names, not a
particular corpus): they raise confidence for names everyone writes, while the
shape, hypernym and cue rules type names no list can contain ("Velmora, a small
town", "Run flumectl sync", "Brannigan approved the change").

A store's ontology may declare other types; those are typed by their own names,
keywords and aliases (and embeddings or an LLM when configured), never by these
built-in lists, which only score types the ontology actually declares."""
from __future__ import annotations

import re


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# --- name lists (lower case; matched against the whole mention) ------------------

FIRST_NAMES = _words("""
aaron abdul abigail adam adrian ahmed aisha akira alan albert alejandro alex alexander alexandra alexis ali
alice alicia alison amanda amara amelia amir amit amy ana andrea andreas andrew andrés angela angelo anil
anita ann anna anne anthony antoine antonio anya arjun arthur ashley aya ayesha barbara ben benjamin bernard
beth betty bianca bill bob brandon brenda brian bruce bruno camila carl carla carlos carmen carol caroline
catherine cecilia charles charlie charlotte chen chloe chris christian christina christine christopher claire
clara claude daniel daniela danielle david deborah deepak denise dennis diana diego dmitri dominic donald
donna dorothy douglas dylan ed eduardo edward elena eli elijah elizabeth ella ellen emily emma eric erik
ethan eva evelyn fatima felipe fernando fiona francesca francisco frank fred gabriel gary george georgia
gerald grace greg gregory hana hannah hans harold harry hassan heather helen henry hiroshi hugo ian igor
ines isaac isabel isabella ivan jack jacob jake james jamie jane janet jason javier jean jeff jeffrey
jennifer jeremy jerry jessica jim joan joanna joe joel john jonathan jordan jorge jose josé joseph joshua
joyce juan julia julian julie justin kai karen karl kate katherine kathleen kathryn katie keith kelly ken
kenji kenneth kevin kim kyle lara larry laura lauren layla leah lena leo leon li liam linda lisa liu lucas
lucia luis luke maria marie mario mark marta martin mary mateo matt matthew maya megan mei melissa michael
michelle miguel mike min mohamed mohammed monica nadia nancy naomi natalia natasha nathan neha nicholas
nicole nina noah noor olga olivia omar oscar pablo pamela patricia patrick paul paula pedro peter philip
pierre pooja priya rachel rahul raj rajesh ramesh raphael rebecca richard rita robert roberto ronald rosa
ruth ryan sakura sam samantha samuel sandra sara sarah scott sean sebastian sergei sergey shirley simon
sofia sophia sophie stephanie stephen steve steven susan takeshi tanya teresa thomas tim timothy tom tony
tyler valentina vanessa victor victoria vikram vincent wang wei william yan yuki yusuf zara zhang
abby adele agnes ahmad aiden akash alberto aleksandr alfred alfredo alicja allison alvaro amina ananya andre
andy angus annie anton april arman arnav arturo asha astrid aurora axel barry beatriz becky benedict bernadette
bethany bilal boris brad brendan brett bridget brooke caleb cameron candice carlo carmen cassandra cathy cesar
chandra chang chiara chidi christoph clarence clark claudia clint colin connor conrad craig curtis cynthia dale
damian dana darius darren dave dawn dean debra derek desmond dina dirk dolores dominik duncan dustin eamon
earl edgar edith edwin eileen elaine eleanor elias elif elise eloise emil emilia emmanuel enrique erin ernest
esther eugene evan ezra fabian faisal farah felix florence francis frederick gareth gavin gemma geoffrey gilbert
gina giovanni giulia glen glenn gloria gordon graham gustavo hamid harriet harvey hector heidi helena herbert
hilda howard ibrahim ida imran irene iris isabelle ivy jacqueline jamal jan janice jared jasmine jay jenna
jesse jill jo joan joanne jodie johan johanna jonas josefina josh joy judith judy julio june kamal karim kasia
katarzyna kayla kelvin kendra kerry kirsten klaus kofi krishna kristen kurt lakshmi lance lars leila lena
leonard leonardo leslie lila lillian lily lionel lorenzo lori louis louise lucy luigi luka lydia mabel madeline
magnus malik manuel marco marcus margaret margarita marina marion marisa marjorie marlene martha mason
matilda maureen max maxine melanie mia milan milo miriam mohan molly morgan muhammad nadine nasser neil
nelson niamh nikhil nikolai noel nora norman olivier oliver omari orlando otto owen paolo parker pascal
patrice paulo pavel penelope percy perry petra phoebe pradeep preeti quentin rafael ralph ramona randy raquel
ravi ray raymond reggie renee rhys ricardo rick rob robin rodrigo roger roland rohan roman ronan rosalind ross
roxanne ruby rupert russell sabrina sadie salma salvatore sana sanjay santiago sasha saul seth shane shannon
sharon sheila shreya sidney silvia simone sonia stan stella stuart sunil suresh sven sylvia tamara tara ted
terry theo theodore tobias todd tomas travis trevor troy ursula uma valerie vera veronica vivian wade walter
wanda warren wendy wesley whitney wilfred willem xavier yara yasmin yolanda yosef yuri yvonne zach zachary
zainab zoe zoltan
""")

PLACES = _words("""
afghanistan albania algeria argentina armenia australia austria azerbaijan bangladesh belarus belgium bolivia
brazil bulgaria cambodia cameroon canada chile china colombia croatia cuba cyprus czechia denmark ecuador
egypt england estonia ethiopia finland france georgia germany ghana greece hungary iceland india indonesia
iran iraq ireland israel italy jamaica japan jordan kazakhstan kenya korea kuwait latvia lebanon lithuania
luxembourg malaysia malta mexico moldova mongolia morocco nepal netherlands nigeria norway pakistan panama
paraguay peru philippines poland portugal qatar romania russia rwanda scotland senegal serbia singapore
slovakia slovenia somalia spain sudan sweden switzerland syria taiwan tanzania thailand tunisia turkey uganda
ukraine uruguay uzbekistan venezuela vietnam wales yemen zambia zimbabwe usa uk uae
africa antarctica asia europe oceania scandinavia
alabama alaska arizona arkansas california colorado connecticut delaware florida hawaii idaho illinois
indiana iowa kansas kentucky louisiana maine maryland massachusetts michigan minnesota mississippi missouri
montana nebraska nevada ohio oklahoma oregon pennsylvania tennessee texas utah vermont virginia washington
wisconsin wyoming ontario quebec bavaria catalonia
amsterdam athens atlanta auckland austin baltimore bangkok barcelona beijing beirut belgrade bengaluru
bangalore berlin bogota boston brisbane brussels bucharest budapest cairo calgary chennai chicago
copenhagen dallas delhi denver detroit dubai dublin edinburgh frankfurt geneva glasgow hamburg hanoi helsinki
honolulu houston hyderabad istanbul jakarta jerusalem johannesburg karachi kathmandu kyiv kyoto
lagos lahore lima lisbon london lyon madrid manchester manila marseille melbourne
miami milan minneapolis montreal moscow mumbai munich nairobi naples
nyc osaka oslo ottawa paris perth philadelphia phoenix portland prague pune
riyadh rome rotterdam santiago seattle seoul shanghai
shenzhen singapore stockholm stuttgart sydney taipei tallinn tokyo toronto turin vancouver venice
vienna warsaw wellington zurich
""") | frozenset({"cape town", "hong kong", "las vegas", "los angeles", "mexico city", "new delhi", "new york",
                 "new york city", "rio de janeiro", "san diego", "san francisco", "san jose", "são paulo",
                 "sao paulo", "tel aviv", "silicon valley", "bay area", "south africa", "south korea",
                 "north korea", "new zealand", "saudi arabia", "united states", "united kingdom",
                 "sri lanka", "costa rica", "puerto rico", "hong kong", "north america", "south america",
                 "latin america", "middle east", "lake tahoe", "mount everest", "new jersey", "new mexico",
                 "north carolina", "south carolina", "rhode island", "west virginia", "british columbia"})

ORGANIZATIONS = _words("""
google alphabet microsoft apple amazon aws meta facebook netflix nvidia intel amd ibm oracle salesforce
adobe cisco sap siemens samsung sony toyota honda tesla spacex uber lyft airbnb stripe shopify spotify
twitter linkedin github gitlab atlassian slack zoom dropbox paypal visa mastercard openai anthropic deepmind
mistral cohere huggingface databricks snowflake cloudflare akamai fastly vercel netlify heroku digitalocean
mozilla wikipedia wikimedia reddit pinterest tiktok bytedance tencent alibaba baidu huawei xiaomi lenovo
dell hp nokia ericsson philips bosch volkswagen bmw mercedes ford gm boeing airbus unilever nestle pfizer
moderna novartis roche walmart target costco ikea mckinsey deloitte accenture kpmg pwc goldman jpmorgan
fda nasa esa nsa fbi cia who unicef unesco nato un eu ecb imf irs sec ftc fcc nhs cern mit stanford harvard
berkeley oxford cambridge caltech princeton yale eth epfl ucla nyu cmu inria
""") | frozenset({"hugging face", "goldman sachs", "jp morgan", "red hat", "general motors", "eli lilly",
                 "procter & gamble", "world bank", "united nations", "european union", "carnegie mellon",
                 "the new york times", "new york times", "linux foundation", "apache foundation"})

TOOLS = _words("""
docker podman kubernetes kubectl helm terraform pulumi ansible chef puppet vagrant packer git mercurial svn
jenkins circleci travis argocd flux spinnaker pytest unittest nose tox nox ruff black isort flake8 pylint
mypy pyright bandit eslint prettier tslint stylelint webpack vite rollup esbuild parcel babel swc turbopack
npm pnpm yarn bun deno pip pipx poetry uv conda cargo rustup maven gradle bazel buck make cmake ninja
alembic sqlalchemy django flask fastapi starlette celery pandas numpy scipy matplotlib seaborn sklearn
scikit-learn pytorch torch tensorflow keras jax onnx transformers langchain llamaindex spacy nltk
react vue angular svelte nextjs next.js nuxt remix express nestjs rails laravel spring springboot junit
mockito jest mocha vitest cypress playwright selenium puppeteer storybook grafana prometheus
opentelemetry jaeger zipkin sentry datadog splunk kibana logstash fluentd vector loki tempo ripgrep grep
sed awk jq yq curl wget httpie postman insomnia ssh tmux vim neovim emacs vscode intellij pycharm xcode
gdb lldb valgrind strace perf htop wireshark tcpdump nmap openssl certbot k9s kustomize skaffold tilt
minikube kind k3s istio linkerd envoy traefik nginx haproxy caddy
""") | frozenset({"github actions", "gitlab ci", "node.js", "vs code", "visual studio", "scikit learn",
                 "hugging face transformers", "react native", "spring boot", "ruby on rails"})

SERVICES = _words("""
postgres postgresql mysql mariadb sqlite redis memcached kafka rabbitmq activemq nats pulsar zookeeper
elasticsearch opensearch solr mongodb cassandra scylladb dynamodb cosmosdb firestore bigtable spanner
cockroachdb clickhouse druid pinot snowflake bigquery redshift athena s3 gcs minio ceph hdfs etcd consul
vault keycloak auth0 okta pgbouncer pgpool sqs sns kinesis pubsub lambda cloudfront route53 ec2 ecs eks gke
aks rds aurora neo4j qdrant pinecone weaviate milvus chroma lancedb pgvector supabase firebase twilio
sendgrid mailgun
""")

EVENTS = _words("""
christmas thanksgiving easter halloween diwali ramadan eid hanukkah newyear
blackfriday cybermonday kubecon pycon wwdc reinvent re:invent neurips icml iclr cvpr acl emnlp ces
mwc defcon blackhat siggraph hackweek hackathon standup retro retrospective offsite allhands all-hands
""") | frozenset({"black friday", "cyber monday", "new year", "new year's eve", "google i/o", "re:invent",
                 "aws re:invent", "apple wwdc", "microsoft build", "google cloud next", "aws summit",
                 "world cup", "super bowl", "the olympics", "olympics", "all hands", "game day"})

ERRORS = _words("""
segfault segmentation deadlock livelock oomkilled oom sigsegv sigkill sigterm sigabrt panic stackoverflow
crashloopbackoff imagepullbackoff errimagepull econnreset econnrefused etimedout enoent eacces eperm eaddrinuse
epipe enomem enospc
""") | frozenset({"stack overflow", "out of memory", "race condition", "memory leak", "null pointer",
                 "segmentation fault", "bus error", "kernel panic", "heap corruption"})

# Base names of files that carry no extension.
FILE_NAMES = frozenset({"dockerfile", "makefile", "jenkinsfile", "procfile", "gemfile", "rakefile", "vagrantfile",
                        "brewfile", "justfile", "license", "codeowners", "readme", "changelog", "pipfile",
                        "containerfile", "tiltfile", "caddyfile"})

LISTS: dict[str, frozenset[str]] = {
    "person": FIRST_NAMES, "place": PLACES, "organization": ORGANIZATIONS, "tool": TOOLS, "service": SERVICES,
    "event": EVENTS, "error": ERRORS,
}
# Some names belong on two lists (snowflake the company and the warehouse; envoy and
# nginx run as services and ship as tools); the list score is lower for those.
AMBIGUOUS = frozenset({"snowflake", "envoy", "nginx", "georgia", "washington", "jordan", "victoria", "chad",
                       "lambda", "vector", "black", "flux", "chef", "puppet", "make", "kind", "tempo",
                       "target", "visa", "cambridge", "oxford", "stanford", "berkeley", "paris", "india",
                       "vercel", "netlify", "heroku", "stripe", "twilio", "firebase", "supabase", "kim",
                       "min", "li", "yan", "mark", "will", "may", "grace", "faith", "hope", "rose", "amber"})

# --- hypernyms: words that name a type in "X is a <word>", "X, a <word>", "<word>s such as X"

HYPERNYMS: dict[str, frozenset[str]] = {
    "person": _words("""person people man woman engineer developer programmer manager director lead founder
        cofounder co-founder ceo cto cfo coo vp intern contractor colleague coworker teammate employee
        designer researcher scientist analyst architect admin administrator user customer-contact author
        creator inventor leader head chair chairperson president assignee reporter approver contact
        maintainer reviewer owner author writer professor student teacher doctor nurse lawyer recruiter
        candidate hire friend boss mentor partner-manager sre on-call sales salesperson representative
        pioneer mathematician physicist entrepreneur philosopher novelist poet journalist"""),
    "place": _words("""place city town village country state province region county district area capital
        island continent neighborhood neighbourhood suburb municipality metropolis lake river mountain
        valley coast port harbor harbour location site campus"""),
    "organization": _words("""organization organisation company firm startup start-up business corporation
        enterprise agency vendor supplier customer client partner competitor team squad department division
        group lab laboratory university college school institute foundation nonprofit non-profit charity
        bank fund government ministry regulator consortium bureau unit org subsidiary"""),
    "tool": _words("""tool library framework sdk cli utility package plugin extension linter formatter
        compiler bundler toolkit toolchain editor ide debugger profiler module dependency language driver
        client wrapper
        command program binary script app application analyzer analyser transpiler interpreter runtime
        emulator simulator scanner"""),
    "service": _words("""service server database db datastore data-store store queue broker cache cluster
        gateway backend microservice endpoint api warehouse index search-engine platform proxy balancer
        load-balancer registry bucket key-value"""),
    "error": _words("""error exception failure fault crash bug issue panic timeout signal warning
        regression""") | frozenset({"error code", "status code", "exit code", "error message"}),
    "event": _words("""event conference summit meetup meeting launch release outage incident migration
        workshop hackathon sprint holiday festival ceremony offsite retreat webinar keynote round funding
        quarter season election celebration deadline milestone kickoff kick-off review"""),
    "symbol": _words("""function method class variable constant field property attribute struct interface
        enum macro syscall decorator getter setter constructor destructor member identifier callback
        coroutine lambda typedef""") | frozenset({"system call", "global variable", "environment variable"}),
    "file": _words("""file files directory folder path filename dotfile"""),
    "document": _words("""document doc docs spec specification rfc readme report runbook handbook policy
        guide guideline manual memo proposal postmortem post-mortem charter contract agreement standard
        paper whitepaper article page wiki note notes plan roadmap design adr decision record template
        checklist playbook book license licence""")
}

# --- mention shapes -------------------------------------------------------------------

_EXT = ("py|pyi|ipynb|js|mjs|cjs|jsx|ts|tsx|go|rs|java|kt|kts|rb|php|c|cc|cpp|cxx|h|hh|hpp|cs|swift|scala|"
        "sql|sh|bash|zsh|ps1|bat|yaml|yml|json|jsonl|toml|ini|cfg|conf|env|md|mdx|rst|txt|csv|tsv|xml|html|"
        "htm|css|scss|sass|less|lock|tf|tfvars|hcl|proto|graphql|gql|dockerfile|gradle|pom|properties|"
        "log|pdf|png|jpg|jpeg|gif|svg|wasm|so|dll|exe|whl|tar|gz|zip|parquet|onnx|pt|pkl|db|sqlite")
SHAPES: tuple[tuple[str, float, re.Pattern[str]], ...] = (
    # (type, score, pattern over the whole mention)
    # A path needs an extension or two separators: "and/or", "TCP/IP" and "24/7" are not files.
    ("file", 0.95, re.compile(r"^(?:\.{0,2}/)?(?:(?:[\w.@-]+/)+[\w@-][\w.@-]*\.[A-Za-z0-9]{1,8}|"
                              r"(?:[\w.@-]+/){2,}[\w.@-]+)$")),
    ("file", 0.92, re.compile(r"^[\w@-][\w.@-]*\.(?:" + _EXT + r")$", re.I)),
    ("file", 0.9, re.compile(r"^\.(?:env|gitignore|dockerignore|editorconfig|npmrc|nvmrc|bashrc|zshrc|"
                             r"profile|pylintrc|flake8|prettierrc|eslintrc)(?:\.[\w-]+)?$", re.I)),
    ("file", 0.92, re.compile(r"^/(?:[\w.@-]+/)+[\w.@-]+$")),  # absolute paths: /etc/passwd
    ("symbol", 0.92, re.compile(r"^[A-Za-z_$][\w$]*(?:(?:\.|::|#|->)[A-Za-z_$][\w$]*)*\(\)$")),
    ("symbol", 0.9, re.compile(r"^(?:[A-Za-z_]\w*::)+[A-Za-z_~]\w*$")),  # std::vector
    ("symbol", 0.9, re.compile(r"^__[a-z][a-z0-9_]*__$")),
    ("symbol", 0.88, re.compile(r"^_?[a-z][a-z0-9]*(?:_[a-z0-9]+)+$")),
    ("symbol", 0.85, re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")),
    ("symbol", 0.85, re.compile(r"^[a-z]+(?:[A-Z][a-z0-9]*)+$")),
    ("symbol", 0.85, re.compile(r"^[A-Za-z_]\w*(?:\.[a-z_]\w*)*\.(?:[a-z]+_[a-z_\d]+|[a-z]+[A-Z]\w*)$")),
    ("symbol", 0.8, re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\.[a-z_]\w*$")),
    # requests.get, os.path.join; two letters a part, so "e.g" and "i.e" are not code
    ("symbol", 0.8, re.compile(r"^[a-z_][a-z0-9_]+(?:\.[a-z_][a-z0-9_]+)+(?<!\.com)(?<!\.org)(?<!\.net)"
                               r"(?<!\.io)(?<!\.dev)(?<!\.ai)(?<!\.js)$")),
    ("error", 0.95, re.compile(r"^[A-Z]\w*(?:Error|Exception|Fault|Panic|Failure)$")),
    # "ZetaSyncMismatch", "VoidPointerLeakSignal": a compound whose last part is a failure noun
    ("error", 0.9, re.compile(r"^[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)*(?:Mismatch|Violation|Timeout|Overflow|"
                              r"Underflow|Deadlock|Corruption|Rejection|Conflict|Signal|Leak|Abort|Halt|"
                              r"Crash|Denied|Refused|Unavailable|NotFound|Exceeded)$")),
    ("error", 0.95, re.compile(r"^(?:HTTP )?[45]\d\d(?: [A-Z][A-Za-z-]+)+$")),  # 404 Not Found
    ("error", 0.92, re.compile(r"^(?:E|ERR|ERROR)_[A-Z0-9_]+$|^[A-Z][A-Z0-9_]*_(?:ERROR|ERR|FAILED|FAILURE|"
                               r"INVALID|MALFORMED|DENIED|TIMEOUT|EXCEEDED|NOT_FOUND|REFUSED|UNAVAILABLE|"
                               r"FORBIDDEN|UNAUTHORIZED|CONFLICT|OVERFLOW)$")),
    ("error", 0.92, re.compile(r"^E(?:CONN\w+|TIMEDOUT|NOENT|ACCES|PERM|ADDRINUSE|PIPE|NOMEM|NOSPC|AGAIN|"
                               r"EXIST|INVAL|ISDIR|NOTDIR|MFILE|HOSTUNREACH|NETUNREACH)$")),
    ("error", 0.9, re.compile(r"^(?:HTTP|status|code)[ ]?[45]\d\d$", re.I)),
    ("error", 0.85, re.compile(r"^(?:[A-Z]{1,3}\d{3,5}|SQLSTATE[ ]?\w{5})$")),
    ("error", 0.9, re.compile(r"^SIG[A-Z]{2,6}$")),
    ("error", 0.88, re.compile(r"^[A-Z][a-zA-Z]+(?:Killed|BackOff|Backoff|Failed|Crashed)$")),
    ("document", 0.95, re.compile(r"^(?:RFC|PEP|ADR|KEP|JEP|BIP|EIP|ISO|IEC|IEEE|NIST SP|FIPS|ECMA)[ -]?\d+"
                                  r"(?:[-.]\d+)*$", re.I)),
    ("place", 0.92, re.compile(r"^(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:north|south|east|west|central|northeast|"
                               r"northwest|southeast|southwest)-\d$")),
    ("place", 0.9, re.compile(r"^(?:us|europe|asia|australia|northamerica|southamerica|me|africa)-[a-z]+\d$")),
    ("event", 0.9, re.compile(r"^(?:Q[1-4]|H[12]|FY ?\d{2,4})(?: \d{4})?(?: (?:planning|launch|review|offsite|"
                              r"kickoff|kick-off|okrs?|roadmap|release|retro|retrospective|all-hands|meeting|"
                              r"summit|earnings|close|freeze|goals|hackathon|sprint|board meeting))?$")),
    ("event", 0.9, re.compile(r"^(?:Sprint|Iteration|Milestone|Release|Week|Cycle|PI) \d+$", re.I)),
    ("event", 0.88, re.compile(r"^(?:[A-Z][\w-]* )+(?:Sprint|Iteration|Milestone|Cycle|PI) \d+$")),
    ("event", 0.6, re.compile(r"^[A-Z][\w:-]*(?: [A-Z][\w:-]*)* (?:19|20)\d{2}$")),  # CodeSync 2024
    ("event", 0.9, re.compile(r"^(?:Series [A-H]|Seed round|Pre-seed|IPO)$", re.I)),
    ("person", 0.95, re.compile(r"^(?:Mr|Mrs|Ms|Mx|Dr|Prof|Sir|Dame)\.? [A-Z][\w'-]+(?: [A-Z][\w'-]+)*$")),
    ("person", 0.85, re.compile(r"^@[A-Za-z0-9][\w-]{1,38}$")),
    ("organization", 0.92, re.compile(r"^[A-Z][\w&.'-]*(?: [A-Z][\w&.'-]*)* (?:Inc|Corp|Corporation|LLC|Ltd|"
                                      r"GmbH|AG|SA|SAS|PLC|BV|NV|Pty|Co|Labs|Technologies|Systems)$")),
    ("tool", 0.9, re.compile(r"^[A-Z][\w-]*\.js$")),  # Node.js, Vue.js: frameworks, not files
    ("service", 0.85, re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*-(?:svc|service|api|server|db|gateway|"
                                 r"proxy|worker|queue|cache|backend|frontend|daemon)$")),
)

# --- context cues: phrases next to the mention ("§" marks it) ----------------------

_N = r"(?:[\w'-]+ ){0,3}"  # up to three words in between
# A cue that opens with a word starts at a word boundary: "within X." is not "in X.".
CUES: tuple[tuple[str, float, re.Pattern[str]], ...] = tuple(
    (type_, score, re.compile(pattern if pattern.startswith("§") else r"(?<![\w-])" + pattern))
    for type_, score, pattern in (
        ("person", 0.8, r"§ (?:often |always |usually |also |never |just |recently )?"
                        r"(?:said|says|told|asked|thinks|thought|believes|believed|prefers|preferred|wants|"
                        r"wanted|approved|reviewed|wrote|joined|left|quit|resigned|retired|signed off|"
                        r"is on call|was on call|is on leave|was promoted|got promoted|replied|commented|"
                        r"mentioned|suggested|proposed|recommended|noted|agreed|disagreed|explained|complained|"
                        r"volunteered|paired|fixed|pushed|merged|reverted|created|founded|co-founded|"
                        r"climbed|moved|relocated|lives|lived|was born|tweeted|emailed|called|emphasizes|emphasized|"
                        r"emphasises|emphasised|argues|argued|insists|insisted|claims|claimed|warned|warns|"
                        r"advocates|advocated|stressed|stresses)\b"),
        ("person", 0.75, r"(?:check|checked|checking|talk|talked|talking|speak|spoke|speaking|chat|chatted|"
                         r"meeting|a call|on a call|met|meet|catch up|caught up|paired|pairing|interview) with §"),
        ("person", 0.75, r"(?:hired|ask|asked|thank|thanks|ping|pinged|cc|email|emailed|call|called|message|"
                         r"messaged|tell|told|meet|met|congratulate|welcome|interview|interviewed|promote|"
                         r"promoted|onboard|onboarded|according to) §"),
        ("person", 0.8, r"§,? who\b"),
        ("person", 0.8, r"§ (?:should|will|can|could|must|would|might|needs to|has to|is going to|wants to|"
                        r"is supposed to) (?:review|approve|merge|sign off|look|check|take|handle|own|lead|join|"
                        r"decide|confirm|weigh in|chime in|pair|help|know|fix|present|own)\b"),
        ("person", 0.8, r"§ to (?:approve|review|sign off|sign|merge|confirm|respond|reply|decide|weigh in)\b"),
        ("person", 0.7, r"(?:send|sent|forward|forwarded|hand|handed|assign|assigned|reassign|reassigned|escalate|"
                        r"escalated|introduce|introduced|pass|passed|give|gave) (?:[\w'-]+ ){0,5}(?:over )?to §"),
        ("person", 0.75, r"(?:leadership|guidance|supervision|direction|approval|sign-off|blessing|mentorship) "
                         r"of §"),
        ("person", 0.65, r"(?:influenced|created|written|designed|invented|authored|reviewed|approved|"
                         r"maintained|led) by §"),
        ("person", 0.75, r"§ (?:[\w'-]+ ){0,4}(?:he|she|his|her|him|hers)\b"),
        ("person", 0.85, r"§, (?:our|their|the|a|an|my|your) " + _N +
                         r"(?:ceo|cto|cfo|coo|vp|founder|engineer|manager|lead|director|intern|designer|"
                         r"developer|researcher|architect|colleague|teammate)\b"),
        ("place", 0.8, r"(?:lives|lived|living|live|moved|relocated|relocating|based|located|headquartered|born|"
                       r"grew up|flew|flying|travel+ed|travel+ing|traveling|commutes?|stationed|stays?|stayed|"
                       r"office|offices|users|customers|team|datacenter|data center|region) (?:in|to|from|near) "
                       r"(?:the )?§"),
        ("place", 0.75, r"(?:the )?§ (?:office|offices|campus|headquarters|hq|branch|datacenter|data center|"
                        r"region|area|airport|metro)\b"),
        ("place", 0.7, r"(?:visit|visited|visiting|in|near|around|outside|across|throughout) (?:the )?§[,.]"),
        ("place", 0.6, r"(?:latency|traffic|users|flights?|trains?|drive|road) (?:from|to) §"),
        ("place", 0.75, r"(?:laws|regulations|government|residents|citizens|weather|time zone|timezone|mayor|"
                        r"council|police|stores?|warehouses?|airport|streets?|population) (?:in|of) §"),
        ("organization", 0.85, r"(?:work|works|worked|working|job|jobs|interned|employed|internship) "
                               r"(?:at|for|by) §"),
        ("organization", 0.8, r"(?:acquired by|acquisition of|partnered with|partnership with|contract with|"
                              r"funded by|backed by|invested in|investors? in|customers? of|clients? of|"
                              r"joined|left) §"),
        ("organization", 0.8, r"§ (?:acquired|raised|announced|released|open-sourced|open sourced|launched|"
                              r"laid off|hired|ships|shipped|sells|sold|bought|signed (?:a |an |the )?"
                              + _N + r"(?:contract|deal|agreement|partnership|lease)|went public|ipo'd|filed|"
                              r"sued|was founded|is headquartered|employs|employed|publishes|published|"
                              r"sponsors|sponsored|invested)\b"),
        ("organization", 0.85, r"§ is (?:our|their|a|an|the) " + _N + r"(?:largest |biggest |main |key )?"
                               r"(?:customer|client|vendor|supplier|partner|competitor|investor|"
                               r"acquirer)\b"),
        ("tool", 0.8, r"(?:use|uses|used|using|run|runs|ran|running|install|installs|installed|installing|"
                      r"upgrade|upgraded|configure|configured|invoke|invoked|launch|via|pip install|"
                      r"npm install|brew install|apt install|cargo install|go install|switch to|migrate to|"
                      r"migrated to) §(?: to| with| for|$|[ .,])"),
        ("tool", 0.75, r"§ (?:flags|flagged|bundles|bundled|formats|formatted|lints|linted|compiles|compiled|"
                       r"transpiles|is configured|was configured|config|plugin|cli|command|version|"
                       r"rule|rules|modules|cache dir)\b"),
        ("tool", 0.65, r"(?:with|in) §(?: modules| scripts| plugins| config|[.,]|$)"),
        ("tool", 0.7, r"(?:rule|rules|plugin|plugins|config|configuration|flag|flags|option|options|extension|"
                      r"preset|presets) (?:to|for|in|of) §"),
        ("service", 0.8, r"§ (?:is|was|went|goes) (?:down|up|unavailable|unreachable|degraded|overloaded|"
                         r"healthy|unhealthy|restarted|redeployed|scaled)\b"),
        ("service", 0.8, r"§ (?:returns|returned|responds|responded|exposes|exposed|listens|serves|served|"
                         r"times out|timed out|stores|stored|holds|caches|cached|replicates|indexes|"
                         r"runs on|hosts|accepts|rejects|throttles|rate-limits) "),
        ("service", 0.8, r"(?:hit|hits|query|queries|queried|call|calls|called|write|writes|written|wrote|"
                         r"read|reads|stored|store|cached|queued|published|uploaded|backed up|flow|flows|"
                         r"routed|sent|connect|connects|connected|persist|persisted|indexed|replicated) "
                         r"(?:\w+ )?(?:to|in|into|from|through|via|on) §"),
        ("service", 0.7, r"(?:push|pushed|enqueue|enqueued|write|writes|writing|load|loads|loading|fetch|fetches|"
                         r"fetching|pull|pulls|stream|streams|streaming|send|sends|sending|upload|uploads|"
                         r"uploading|dump|dumps|sync|syncs|syncing|replicate|replicating|store|storing|coming|"
                         r"comes|serve|serving|proxy|proxied|route|routing) (?:[\w'-]+ ){0,3}"
                         r"(?:to|into|from|in|via|through|over to) §"),
        ("service", 0.7, r"(?:integration|integrations|webhooks?|sdk|client|connections?|calls?|requests?|"
                         r"queries|outage|downtime|quota|credentials|api keys?) (?:with|to|from|for|against|on) §"),
        ("service", 0.75, r"(?:restart|restarted|deploy|deployed|scale|scaled|provision|provisioned|"
                          r"query|queried|hit|hits|rely on|relies on|depend on|depends on) (?:the )?§"),
        # "raised a KeyError", "fails with ECONNRESET"; "Raise MAX_RETRIES to 5" is a setting.
        ("error", 0.85, r"(?:raise|raises|raised|throw|throws|threw|thrown) (?:a |an |the )§"),
        ("error", 0.85, r"(?:raise|raises|raised|throw|throws|threw|thrown) §(?! to \d)"),
        ("error", 0.85, r"(?:hit|hits|hitting|see|sees|seeing|saw|get|gets|got|getting|report|reports|reported|"
                        r"catch|catches|caught|fail|fails|failed|failing|crash|crashes|crashed|die|dies|died|"
                        r"abort|aborts|aborted) (?:with |on |from )?(?:a |an |the )?§"),
        ("error", 0.75, r"(?:caused|causes|cause|causing|trigger|triggers|triggered|fix|fixes|fixed|avoid|"
                        r"prevent|prevents|prevented) (?:a |an |the )?§"),
        ("error", 0.75, r"§ (?:crashes|crashed|occurs|occurred|happens|happened|is raised|was raised|"
                        r"is thrown|was thrown|keeps happening)\b"),
        ("event", 0.75, r"(?:during|throughout|ahead of|attend|attended|attending|at the time of) "
                        r"(?:the )?§(?:[ .,]|$)"),
        ("event", 0.6, r"(?:before|after|since|over) (?:the )?§(?:[ .,]|$)"),
        ("event", 0.85, r"§ (?:starts|started|begins|began|ends|ended|kicks off|kicked off|takes place|"
                        r"took place|was held|is held|is scheduled|was scheduled|was postponed|"
                        r"was cancelled|was canceled)\b"),
        ("event", 0.75, r"(?:demoed|presented|spoke|talked|announced|keynote|booth) (?:[\w'-]+ ){0,3}(?:at|during) "
                        r"(?:the )?§"),
        ("event", 0.85, r"(?:closed|raised|led|announced) (?:the |a |our |their )?§ (?:round|in)\b"),
        ("event", 0.8, r"(?:registration|register|tickets?|talks?|sessions?|attendees|speakers|keynote|agenda|"
                       r"booth|cfp|venue|recap|after-party) (?:for|at|of|to) (?:the )?§"),
        ("event", 0.7, r"(?:travel+ing|registered|signed up|preparing|prepping|heading out|leaving) for "
                       r"(?:the )?§"),
        ("event", 0.7, r"(?:carried over|left over|rolled over|spilled over) from (?:the )?§"),
        ("document", 0.8, r"§ (?:defines?|defined|specif(?:y|ies)|specified|recommends?|recommended|records|"
                          r"recorded|describes?|described|proposes?|proposed|lists|listed|forbids?|forbade|"
                          r"requires|mandates|outlines|documents|states|covers|explains|summarizes|summarises)\b"),
        ("document", 0.75, r"(?:read|reading|follow|following|see|per|according to|as described in|"
                           r"defined in|documented in|draft|drafted|wrote|written|publish|published|update|"
                           r"updated|edit|edited|review|reviewed|reviewing|sign|signed|link to|linked|cite|"
                           r"cites|cited|citing|quote|quoted|consult|consulted|licensed under) (?:the )?§"),
        ("document", 0.75, r"(?:section|chapter|page|clause|paragraph|appendix|footnote|excerpt|passage|draft|"
                           r"revision|author|authors|editor|sign-off) (?:[\w'-]+ ){0,4}(?:in|of|from) "
                           r"(?:the )?§"),
    )
)

# Words that never name a person, place or organization on their own however the
# context reads: abstract nouns ("Kindness goes a long way", "Latency matters").
ABSTRACT_SUFFIX = re.compile(r"(?:ity|ness|ism|ance|ence|ancy|ency|ship|hood|ment|tion|sion|ing|acy|ics)$")

# Frequent English words: a capitalised one at a sentence start ("Move", "Search",
# "Nightly") is an ordinary word, not a name, and opens no multi-word name.
COMMON_WORDS = _words("""
a about above across act add added adding after again against ago all allow almost along already also
although always am among an and another any anyone anything are around as ask asked at avoid away back
bad be because become been before being below best better between big both bring build built but by call
can cannot case change check choose clean clear close code come could create current daily data day did
different do does doing done down drop during each early easy either else empty enable end enough ensure
even ever every everyone everything example except fail failing fast few find first fix follow for force
format found free from full get give given go goes going good got great had handle has have having he
help her here high his hold hot how however i if important in include instead into is it its just keep
kind know large last later least less let like limit line little load local long look low made main make
many may maybe me might more most move much must my need needs never new next nightly no none not note
nothing now of off often old on once one only open or order other others our out over own pass passing
per place plan please plus prefer put quick quickly rather raise read ready real really recent remove
replace request requests require return right run same save say search secrets see seem send set
several she should show since slow small so some something sometimes soon start still stop such sure
switch take test than thanks that the their them then there these they thing things think this those
though through time to today together too try turn two under unless until up update upon us use used
user users using usually very via want was watch way we weekly well were what when where whether which
while who whom why will with within without work would write yes yet you your
""")
