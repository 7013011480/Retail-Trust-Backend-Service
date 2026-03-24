# Hudson v2 → Production: Scaling Guide

> From a single-user CLI to a product serving thousands of users.

---

## Table of Contents
- [Part 1: Reality Check](#part-1-reality-check--where-you-are-vs-where-you-need-to-be)
- [Part 2: Model Serving & Inference](#part-2-model-serving--inference-layer)
- [Part 3: Token Budget Management](#part-3-token-budget-management)
- [Part 4: Context Management Strategies](#part-4-context-management-strategies)
- [Part 5: Observability](#part-5-observability--the-internal-and-external-view)
- [Part 6: Architecture for 1000+ Users](#part-6-architecture-for-1000-users)
- [Part 7: Cost Model](#part-7-the-cost-model--what-youd-charge)
- [Part 8: Priority Roadmap](#part-8-priority-roadmap)

---

## Part 1: Reality Check — Where You Are vs Where You Need to Be

Right now Hudson is:
- **Single user**, single machine, single Ollama instance
- **No persistence** — conversation dies when CLI closes
- **No auth**, no multi-tenancy, no rate limiting
- **Synchronous** — one request blocks everything
- **No cost tracking** — you don't know how many tokens a query costs

To serve thousands of users, every one of these becomes a problem. Let's break it down layer by layer.

---

## Part 2: Model Serving & Inference Layer

This is the biggest cost and bottleneck. One Ollama instance serving one user at a time won't scale.

### Option A: Self-Hosted Inference (Scaled Up)

```
Users → Load Balancer → [vLLM Instance 1]
                      → [vLLM Instance 2]
                      → [vLLM Instance 3]
```

**Switch from Ollama to vLLM or TGI (Text Generation Inference)**:

| | Ollama | vLLM | TGI |
|---|--------|------|-----|
| Concurrent requests | 1 (sequential) | Many (continuous batching) | Many |
| KV cache management | Basic | PagedAttention (efficient) | Good |
| Throughput (tokens/sec) | ~30-50 | ~200-500 | ~150-400 |
| Multi-GPU | Limited | Native | Native |
| Production ready | No | Yes | Yes |

**The math for 1000 concurrent users:**
- Average query: ~2000 input tokens + ~1000 output tokens
- Average tool loop: 2-3 LLM calls per user query
- So ~6000-9000 tokens per user query
- At 50 tok/s per Ollama instance: one query takes ~60-180 seconds
- At 400 tok/s per vLLM instance with batching: one query takes ~8-15 seconds
- For 1000 users with 10 queries/hour each: you need ~3-5 GPU nodes with vLLM vs ~50+ with Ollama

**GPU math (Qwen3.5 9B at FP16):**
- Model weights: ~18GB VRAM
- KV cache per user (32K context): ~2-4GB
- One A100 80GB: model (18GB) + ~15 concurrent KV caches
- One RTX 4090 24GB: model (18GB) + ~1-2 concurrent KV caches

> **Recommendation:** If self-hosting, use vLLM on A100s or H100s. RTX 5060 Ti is great for dev, not for prod.

### Option B: API Provider (Simpler, Variable Cost)

Use an API provider instead of self-hosting:
- **Together.ai** / **Fireworks.ai** — host open models like Qwen at ~$0.20-0.60 per million tokens
- **Groq** — ultra-fast inference, limited model selection
- **Anthropic/OpenAI** — better models, higher cost ($3-15 per million tokens)

**The cost math for 1000 users:**
```
1000 users x 10 queries/day x ~8000 tokens/query = 80M tokens/day

At $0.30/M tokens (Together.ai, Qwen class):  ~$24/day  = ~$720/month
At $3.00/M tokens (Claude Sonnet class):       ~$240/day = ~$7200/month
```

This is where token budget management becomes critical.

### Option C: Hybrid (Best of Both)

- Self-host a small model (Qwen 9B) for simple queries, tool routing, summarization
- Route complex queries to a larger API model (Claude, GPT-4o)

```
User Query → Router (local, fast)
              ├→ Simple/tool-call → Local Qwen 9B (cheap)
              └→ Complex/synthesis → Claude API (expensive, accurate)
```

---

## Part 3: Token Budget Management

This is where most agent developers lose money. You need to track and control every token.

### Token Accounting — Know What You're Spending

Every LLM call in Hudson currently has hidden costs:

```
Call 1 (initial):
  System prompt:           ~800 tokens
  Tool schemas (16 tools): ~2500 tokens   ← THIS IS HUGE
  User message:            ~50 tokens
  ─────────────────────────────────────
  Input:                   ~3350 tokens
  Output:                  ~100 tokens (tool call JSON)

Call 2 (after tool):
  System prompt:           ~800 tokens
  Tool schemas:            ~2500 tokens
  User message:            ~50 tokens
  Tool call:               ~100 tokens
  Tool result:             ~1000 tokens
  ─────────────────────────────────────
  Input:                   ~4450 tokens
  Output:                  ~500 tokens (answer)

Total for ONE simple query: ~8400 tokens
```

**The tool schema tax is 2500 tokens on EVERY call.** With 3 LLM calls per query, that's 7500 tokens just for schemas. For 1000 users x 10 queries/day = 75M wasted tokens/day on schemas alone.

### Strategy 1: Dynamic Tool Loading

Don't send all 16 tool schemas every call. Route first, then load only relevant tools.

```python
# Instead of binding all 16 tools:
llm.bind_tools(ALL_TOOLS + SKILL_TOOLS)  # 2500 tokens every call

# Route first with a lightweight call (no tools bound):
intent = classify_intent(user_message)  # ~200 tokens total

# Then bind only what's needed:
if intent == "books":
    llm.bind_tools([search_books, web_search])  # ~400 tokens
elif intent == "movies":
    llm.bind_tools([search_movies, get_movie_details, web_search])  # ~500 tokens
elif intent == "research":
    llm.bind_tools([deep_research])  # ~200 tokens
```

**Savings: 60-80% of schema tokens per call.**

The intent classifier can be:
- A tiny local model (Qwen 0.5B or a fine-tuned classifier)
- A regex/keyword router (zero tokens, deterministic)
- A cached classifier (same intent pattern = same routing)

### Strategy 2: Prompt Compression

Your system prompt is ~800 tokens. For returning users, much of it is repeated context.

```python
# First message: full system prompt (800 tokens)
# Subsequent messages in same session: compressed prompt (200 tokens)

COMPRESSED_PROMPT = """You are Hudson. Be witty, use tools, ground answers in tool results.
Do NOT fake tool calls. ACT don't narrate. One tool per turn."""
```

**Savings: ~600 tokens per follow-up call x 2-3 calls per query.**

### Strategy 3: Tool Output Compression

You already truncate at 4000 chars. Go further — summarize tool output before feeding it back to the LLM.

```python
def compress_tool_output(tool_name: str, raw_output: str) -> str:
    """Extract only the essential data from tool output."""
    if tool_name == "search_books":
        # Strip chunk text, keep title + author + first 200 chars
        # Raw: 4000 chars -> Compressed: 500 chars
        ...
    elif tool_name == "deep_research":
        # Strip section headers, keep key facts
        # Raw: 8000 chars -> Compressed: 2000 chars
        ...
    elif tool_name == "get_movie_details":
        # Keep title, director, rating, synopsis
        # Drop full cast list, production companies, etc.
        ...
```

**Savings: 50-70% of tool result tokens.**

### Strategy 4: Token Budget Per Query

Set a hard budget and track it:

```python
class TokenBudget:
    def __init__(self, max_tokens: int = 15000):
        self.max_tokens = max_tokens
        self.used = 0

    def can_afford(self, estimated_cost: int) -> bool:
        return self.used + estimated_cost <= self.max_tokens

    def spend(self, tokens: int):
        self.used += tokens

    def remaining(self):
        return self.max_tokens - self.used

# In the agent loop:
budget = TokenBudget(max_tokens=15000)

# Before each LLM call:
estimated = len(messages) * 4 + schema_tokens  # rough estimate
if not budget.can_afford(estimated):
    # Force synthesis with remaining budget
    inject_stop_message()
```

This replaces the current `tool_count` cap with a smarter **token-based** cap.

---

## Part 4: Context Management Strategies

The current `summarize_and_trim` is a good start. Here's the full spectrum:

### Level 1: Sliding Window
Keep last N messages, drop the rest. Simple but loses context.
```
[msg1, msg2, msg3, msg4, msg5, msg6, msg7, msg8]
                              | (keep last 4)
                    [msg5, msg6, msg7, msg8]
```
**Problem:** User said something important in msg2 — now it's gone.

### Level 2: Summarize and Trim (Current Hudson approach)
Summarize old messages, keep recent ones.
```
[summary_of_msg1-4, msg5, msg6, msg7, msg8]
```
**Problem:** Summary costs a full LLM call (~3000 tokens input + ~500 output). If you summarize every 6 messages, that's an extra LLM call every 3 user turns.

### Level 3: Hierarchical Memory (Recommended next step)

```
+--------------------------------------------------+
|  Working Memory (current turn)                   |
|  Last 4 messages — full detail                   |
|  Budget: ~4000 tokens                            |
+--------------------------------------------------+
|  Short-term Memory (this session)                |
|  Summarized older turns from this conversation   |
|  Budget: ~1000 tokens                            |
+--------------------------------------------------+
|  Long-term Memory (cross-session)                |
|  User preferences, past topics, key facts        |
|  Stored in DB, retrieved by relevance            |
|  Budget: ~500 tokens                             |
+--------------------------------------------------+
|  System Context (always present)                 |
|  System prompt + tool schemas                    |
|  Budget: ~3000 tokens (compressed) to ~5500      |
+--------------------------------------------------+
Total budget per call: ~8500-11000 tokens input
Leaves ~20000 tokens for output + safety margin
```

Implementation:

```python
class HierarchicalMemory:
    def __init__(self, user_id: str):
        self.working = []          # last 4 messages (full)
        self.short_term = ""       # session summary
        self.long_term = []        # from database
        self.user_id = user_id

    def build_context(self, system_prompt: str, tool_schemas: list) -> list:
        """Build the full message list for an LLM call."""
        messages = []

        # 1. System context
        context = system_prompt
        if self.short_term:
            context += f"\n\nSession so far: {self.short_term}"
        if self.long_term:
            context += f"\n\nUser context: {' '.join(self.long_term)}"
        messages.append(SystemMessage(content=context))

        # 2. Working memory (recent messages)
        messages.extend(self.working)

        return messages

    def add_message(self, msg):
        self.working.append(msg)
        if len(self.working) > 8:
            # Promote oldest to short-term summary
            old = self.working[:4]
            self.working = self.working[4:]
            self.short_term = self._summarize(old, self.short_term)

    def save_to_db(self):
        """Persist long-term memory after session ends."""
        # Extract key facts from session
        # Store in vector DB for retrieval in future sessions
        ...
```

### Level 4: RAG Over Conversation History (Advanced)

For power users with long histories, don't summarize — **index**.

```
User asks about "that movie we discussed last week"
    -> Embed the query
    -> Search conversation history vector index
    -> Retrieve relevant past turns
    -> Inject as context
```

This requires a vector store (ChromaDB, Qdrant, Pinecone) per user, but it means you never truly "forget" anything.

---

## Part 5: Observability — The Internal and External View

### Internal View (Developer Dashboard)

You need to see what's happening inside the agent at all times.

**Per-request metrics:**
```
request_id:   "req_abc123"
user_id:      "user_456"
query:        "deep dive into existentialism"
timestamp:    "2026-03-24T14:30:00Z"

llm_calls: 3
  call_1: {input_tokens: 3400, output_tokens: 85,  latency_ms: 2100, tool_called: "deep_research"}
  call_2: {input_tokens: 7200, output_tokens: 120, latency_ms: 3400, tool_called: "search_books"}
  call_3: {input_tokens: 8800, output_tokens: 950, latency_ms: 8200, tool_called: null}

total_tokens:     24055
total_latency_ms: 13700
tool_calls:       ["deep_research", "search_books"]
tool_errors:      0
budget_used:      24055 / 30000 (80.2%)
context_at_peak:  8800 tokens
```

**Aggregate metrics (dashboard):**
```
-- Token Usage ----------------------------
Today:          12.4M tokens
  Schema tax:    4.1M (33%)    <-- optimization target
  Tool results:  3.8M (31%)
  LLM output:    2.9M (23%)
  System prompt:  1.6M (13%)

-- Latency --------------------------------
p50: 4.2s    p95: 18.1s    p99: 45.3s

-- Tool Usage -----------------------------
search_books:   34%    (success: 78%)
web_search:     28%    (success: 95%)
deep_research:  15%    (success: 89%)
search_movies:   12%   (success: 61%)  <-- TMDB flaky
calculator:      5%    (success: 100%)
others:          6%

-- Error Budget ---------------------------
EOF crashes:       0.3% of requests
Malformed tools:   1.2% of requests
Tool cap hits:     8.4% of requests  <-- users hitting 6-call limit
Hallucinations:    ~5% estimated (hard to measure)

-- Cost -----------------------------------
Today: $24.80 (80M tokens @ $0.31/M)
Per user per day: $0.025
```

**Implementation — extend the existing tracer:**

```python
class ProductionTracer:
    def __init__(self):
        self.metrics_backend = None  # Prometheus, DataDog, etc.

    def on_llm_start(self, request_id, input_tokens, tools_bound):
        self.metrics_backend.histogram("llm.input_tokens", input_tokens)
        self.metrics_backend.gauge("llm.tools_bound", len(tools_bound))

    def on_llm_end(self, request_id, output_tokens, latency_ms, tool_calls):
        self.metrics_backend.histogram("llm.output_tokens", output_tokens)
        self.metrics_backend.histogram("llm.latency_ms", latency_ms)
        for tc in tool_calls:
            self.metrics_backend.counter(f"tool.called.{tc}")

    def on_tool_error(self, request_id, tool_name, error):
        self.metrics_backend.counter(f"tool.error.{tool_name}")

    def on_budget_exceeded(self, request_id, used, limit):
        self.metrics_backend.counter("budget.exceeded")

    def on_request_complete(self, request_id, total_tokens, total_latency):
        self.metrics_backend.histogram("request.total_tokens", total_tokens)
        self.metrics_backend.histogram("request.total_latency", total_latency)
```

**Stack:** Prometheus + Grafana, or DataDog, or even a simple SQLite + a dashboard page.

### External View (User-Facing)

Users need to understand:
- **What Hudson is doing** (tool calls in progress)
- **Why it's taking time** (searching 50 books, querying web)
- **When something fails** (graceful error messages)
- **What it costs them** (if billing per query)

The current CLI already shows tool calls. For a web product, this becomes a streaming UI:

```
User: "deep dive into existentialism"

Hudson is thinking...
  -> Searching books library... done
  -> Searching the web... done
  -> Searching movies... done (2 found)
  -> Searching music... done (0 found)

Hudson: [streaming response appears here]

-- 8,240 tokens used . 4.2 seconds --
```

---

## Part 6: Architecture for 1000+ Users

### From CLI to API Service

```
Current:
  User -> CLI -> LangGraph -> Ollama (local)

Production:
  Users -> API Gateway (FastAPI / nginx)
            -> Auth (JWT / API keys)
            -> Rate Limiter (Redis)
            -> Queue (Celery / RQ / Bull)
            -> Worker Pool
                -> LangGraph instances
                -> Inference Backend (vLLM cluster)
            -> Storage (Postgres + Redis + Vector DB)
            -> Observability (Prometheus + Grafana)
```

### Key Components

**1. API Layer (FastAPI)**
```python
@app.post("/v1/chat")
async def chat(request: ChatRequest, user: User = Depends(auth)):
    # Rate limit check
    if not rate_limiter.allow(user.id):
        raise HTTPException(429, "Rate limit exceeded")

    # Token budget check
    budget = get_user_budget(user.id)
    if budget.remaining <= 0:
        raise HTTPException(402, "Token budget exhausted")

    # Queue the request
    job = queue.enqueue(run_agent, user.id, request.message)

    # Stream response
    return StreamingResponse(stream_job_output(job))
```

**2. State Persistence (Postgres + Redis)**
```
Postgres:
  users           -- id, plan, token_budget, created_at
  conversations   -- id, user_id, created_at
  messages        -- id, conversation_id, role, content, tokens, created_at
  tool_calls      -- id, message_id, tool_name, args, result, latency_ms
  metrics         -- request_id, tokens_in, tokens_out, latency, cost

Redis:
  session:{user_id}      -- current conversation state (fast access)
  rate_limit:{user_id}   -- sliding window counter
  cache:{query_hash}     -- cached tool results (TTL: 1 hour)
```

**3. Caching Layer — This Saves the Most Money**

```
+---------------------------------------------+
|         Caching Hierarchy                    |
+---------------------------------------------+
|                                              |
|  L1: Exact Query Cache (Redis)              |
|      "best nolan movies" -> cached response  |
|      Hit rate: ~5-10%                        |
|      Savings: 100% of tokens on hit          |
|                                              |
|  L2: Semantic Cache (Vector DB)             |
|      "top christopher nolan films"           |
|       ~ "best nolan movies" -> cache hit     |
|      Hit rate: ~15-25%                       |
|      Savings: 100% of tokens on hit          |
|                                              |
|  L3: Tool Result Cache (Redis, TTL)         |
|      search_movies("nolan") -> cached        |
|      Hit rate: ~30-50%                       |
|      Savings: tool call latency + tokens     |
|                                              |
|  L4: KV Cache (vLLM)                        |
|      Prefix caching for system prompt        |
|      Automatic with vLLM                     |
|      Savings: ~30% of input processing       |
|                                              |
+---------------------------------------------+
```

**Semantic cache alone can cut costs by 20-25%.** Many users ask similar questions.

```python
class SemanticCache:
    def __init__(self, vector_db, similarity_threshold=0.95):
        self.db = vector_db
        self.threshold = similarity_threshold

    def get(self, query: str) -> Optional[str]:
        embedding = embed(query)
        results = self.db.search(embedding, top_k=1)
        if results and results[0].score >= self.threshold:
            return results[0].cached_response
        return None

    def set(self, query: str, response: str, ttl: int = 3600):
        embedding = embed(query)
        self.db.upsert(embedding, response, ttl=ttl)
```

---

## Part 7: The Cost Model — What You'd Charge

```
-- Free Tier ----------------------------------
Queries/day:     10
Token budget:    100K tokens/day
Tools:           Basic (books, web, calculator)
Context:         Single session (no memory)

-- Pro Tier ($9.99/month) ---------------------
Queries/day:     100
Token budget:    2M tokens/day
Tools:           All (including deep_research, delegate)
Context:         Persistent memory across sessions
Priority:        Standard queue

-- Team Tier ($29.99/month) -------------------
Queries/day:     500
Token budget:    10M tokens/day
Tools:           All + custom tool integration
Context:         Shared team knowledge base
Priority:        Fast queue
API access:      Yes
```

**Unit economics at Pro tier:**
```
Revenue:  $9.99/user/month
Cost:     100 queries/day x 30 days x 8000 tokens x $0.30/M = $7.20/user/month
Margin:   $2.79/user/month (28%)
```

That's thin. This is why caching and token optimization matter — every token saved is pure margin.

---

## Part 8: Priority Roadmap

### Phase 1: Foundation (Week 1-2)
- [ ] Switch CLI to FastAPI with streaming endpoint
- [ ] Add Postgres for conversation persistence
- [ ] Add Redis for session state and caching
- [ ] Add per-request token counting in tracer
- [ ] Add tool result caching (L3 cache)

### Phase 2: Efficiency (Week 3-4)
- [ ] Implement dynamic tool loading (intent router)
- [ ] Compress system prompt for follow-up calls
- [ ] Compress tool outputs before feeding to LLM
- [ ] Add token budget per user per request
- [ ] Implement semantic cache (L2)

### Phase 3: Scale (Week 5-6)
- [ ] Switch from Ollama to vLLM
- [ ] Add worker queue (Celery or similar)
- [ ] Add auth + rate limiting
- [ ] Build Grafana dashboard for metrics
- [ ] Load test with simulated users

### Phase 4: Intelligence (Week 7-8)
- [ ] Hierarchical memory (working -> short-term -> long-term)
- [ ] Smart routing (simple queries -> small model, complex -> large model)
- [ ] User feedback loop (thumbs up/down -> fine-tuning data)
- [ ] A/B testing framework for prompt variants

---

## Summary: Biggest Wins in Order

1. **Caching** — 20-50% cost reduction with minimal effort
2. **Dynamic tool loading** — 60-80% schema token savings
3. **Token compression** — 50-70% tool result savings
4. **Better inference backend (vLLM)** — 5-10x throughput
5. **Hierarchical memory** — smarter context, fewer wasted tokens
