# Agents for Humans: teaching Happy to remember incidents with AgentCore Memory

The second time checkout gets OOM-killed this week, I don't want Happy to investigate it from
zero again. I want it to say "seen this, rollback fixed it in 4 minutes last time" and propose
that fix first — with the evidence, and still with an approval gate, because remembering a fix
worked before is not the same thing as being trusted to apply it unsupervised. This post is about
how that recall works: fingerprinting, the incident ledger, and why the learning loop is designed
so that no amount of successful history ever promotes a tool off the tier it started on.

## Fingerprinting: same failure, same signature

Two occurrences of the same OOM never look identical at the log level. Different pod names,
different timestamps, different byte offsets. If Happy compared raw log lines, it would never
recognize a repeat. So the first step strips everything volatile out of an error-ish line before
comparing anything:

```python
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}...\b", re.IGNORECASE)
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-...-[0-9a-f]{12}\b", re.IGNORECASE)
_POD_SUFFIX_RE = re.compile(r"-[a-z0-9]{5,10}-[a-z0-9]{5}\b", re.IGNORECASE)

def normalize_line(line: str) -> str:
    text = line
    text = _TIMESTAMP_RE.sub("<ts>", text)
    text = _UUID_RE.sub("<uuid>", text)
    text = _IPV4_RE.sub("<ip>", text)
    text = _POD_SUFFIX_RE.sub("-<pod>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _INT_RE.sub("<n>", text)
    return _WHITESPACE_RE.sub(" ", text.lower()).strip()
```

The order matters more than it looks like it should — pod-name suffixes have to be stripped
before the generic hex pattern, or a replicaset hash gets chewed up piecemeal instead of
collapsing cleanly to one placeholder. Once every error-ish line is normalized, `top_signatures`
keeps the most frequent shapes, and `incident_fingerprint` hashes the service, namespace, failure
reason, and those sorted signatures into a 10-character id:

```python
def incident_fingerprint(service, namespace, reason, log_lines) -> Fingerprint:
    signatures = top_signatures(log_lines)
    basis = "|".join([service, namespace, reason, "|".join(sorted(signatures))])
    fingerprint_id = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
    return Fingerprint(id=fingerprint_id, service=service, namespace=namespace,
                        reason=reason, signatures=signatures)
```

Two OOM runs on `checkout`, weeks apart, with completely different pod names, produce the same
id. An OOM and a `CrashLoopBackOff` on the same service produce different ones, because the
signatures underneath them don't overlap. That stability is the entire point — the fingerprint is
the key everything else in the learning loop is indexed by.

## The ledger: recall, don't duplicate

The ledger sits behind one interface with two backends — a JSON file for local runs, and
`MemoryClient` from `bedrock_agentcore.memory` for the deployed agent, writing into
`happyMemory`. Recall checks the exact fingerprint id first, then falls back to signature overlap
above a threshold, so a near-miss (a slightly different log line mix on the same underlying
failure) still surfaces a related record instead of nothing:

```python
@staticmethod
def _find_similar(document: LedgerDocument, fp: Fingerprint) -> list[IncidentRecord]:
    exact = document.records.get(fp.id)
    if exact is not None:
        return [exact]
    scored = []
    for record in document.records.values():
        overlap = _jaccard(fp.signatures, record.fingerprint.signatures)
        if overlap >= _SIMILARITY_THRESHOLD:
            scored.append((overlap, record))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [record for _, record in scored]
```

Remembering an incident that's already in the ledger increments a counter rather than appending a
new row — "checkout OOM, 3rd time" is one record with `count=3`, not three records someone has to
mentally merge. On AgentCore Memory, the same event also gets written as a short natural-language
sentence into a separate session, which is what lets the semantic strategy extract it into
`/users/{actorId}/facts` for later retrieval:

```python
fact = (
    f"Incident {fp.service}/{fp.reason} in namespace {fp.namespace} has now happened "
    f"{record.count} times. Latest summary: {summary}"
)
self._remember_fact(fact)
```

Four memory strategies run on `happyMemory`: semantic facts for exactly this kind of recall, user
preferences for denial reasons, summarization so long investigations don't blow the context
window, and episodic memory with a reflection namespace for cross-session patterns. The ledger
uses the first two directly; the runner leans on summarization for anything that runs long.

## Outcomes: memory earns its trust, one recovery at a time

A recognized signature isn't proposed with confidence just because Happy has *seen* it before —
it has to have actually *worked* before. After every fix, the runner polls cluster health for up
to five minutes and records what happened:

```python
def record_outcome(self, record_id, action, succeeded, minutes_to_recover) -> None:
    document = self._load()
    self._apply_record_outcome(document, record_id, action, succeeded, minutes_to_recover)
    self._save(document)
```

`successful_fixes` then ranks a record's past actions by whether they succeeded at all, then by
success count, then by recency — a fix that failed once doesn't get proposed with the same
confidence as one that's worked twice in a row, and a fix that's never succeeded doesn't get
proposed as the confident first move regardless of how often it's been tried. This is where the
loop earns the word "learns" instead of just "remembers": the ledger isn't a cache of things
Happy has said, it's a scoreboard of things that actually resolved the incident.

Preferences work the same way but from the human side. A deny with a real reason — not just
"deny 4a2f1" with nothing after it — gets stored verbatim and folded into future proposals for
that service:

```python
def remember_preference(self, text: str) -> None:
    document = self._load()
    document.preferences.append(text)
    self._save(document)
    self._remember_fact(f"Preference: {text}", role="USER")
```

## Why Happy never learns to skip you

This is the line I keep coming back to when people ask what happens once the ledger has a long
enough track record: nothing changes about who gets asked. A signature with ten prior successes
still goes through the same `APPROVAL` interrupt as the first time it ever occurred. What the
ledger buys is a *better proposal* — the right fix, first, with the evidence that it worked
before — not a *skipped step*. There is no tier in `guardrails.TIERS` that a track record can
promote a tool into, and no code path where `recall_similar` returning a confident match causes
`require_approval` to be bypassed. The two systems don't talk to each other in that direction on
purpose.

That's a deliberate, slightly contrarian design choice for something billed as a learning loop.
The instinct with "the agent has done this ten times successfully" is to eventually stop asking.
I think that instinct is exactly backwards for anything touching production: the fix that's
correct nine times can still be the wrong call on the tenth, for reasons the fingerprint can't
see — a maintenance window, a dependent deploy in flight, a human who just knows something the
logs don't say. The learning loop makes Happy faster at being right. It was never supposed to
make Happy harder to override.
