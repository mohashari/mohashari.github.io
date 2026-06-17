import os

svg_content = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 950 650" width="100%" height="100%" style="background-color: #0f172a; font-family: 'Inter', system-ui, -apple-system, sans-serif;">
  <defs>
    <!-- Gradients -->
    <linearGradient id="client-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#0284c7" />
    </linearGradient>
    <linearGradient id="server-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#818cf8" />
      <stop offset="100%" stop-color="#4f46e5" />
    </linearGradient>
    <linearGradient id="worker-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fb7185" />
      <stop offset="100%" stop-color="#e11d48" />
    </linearGradient>
    <linearGradient id="redis-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#f43f5e" />
      <stop offset="100%" stop-color="#be123c" />
    </linearGradient>
    <linearGradient id="postgres-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#34d399" />
      <stop offset="100%" stop-color="#059669" />
    </linearGradient>
    <linearGradient id="llm-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#a855f7" />
      <stop offset="100%" stop-color="#7c3aed" />
    </linearGradient>
    <linearGradient id="bg-grad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#1e293b" />
      <stop offset="100%" stop-color="#0f172a" />
    </linearGradient>

    <!-- Drop Shadows -->
    <filter id="shadow" x="-5%" y="-5%" width="110%" height="110%">
      <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="#000000" flood-opacity="0.5" />
    </filter>

    <!-- Arrow Marker -->
    <marker id="arrow" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#94a3b8" />
    </marker>
    <marker id="arrow-blue" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#38bdf8" />
    </marker>
    <marker id="arrow-purple" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#c084fc" />
    </marker>
    <marker id="arrow-rose" viewBox="0 0 10 10" refX="6" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M 0 1 L 10 5 L 0 9 z" fill="#fb7185" />
    </marker>
  </defs>

  <!-- Title & Background -->
  <rect width="100%" height="100%" fill="url(#bg-grad)" />
  <text x="30" y="40" fill="#f8fafc" font-size="20" font-weight="bold" letter-spacing="0.5">LangGraph &amp; WebSockets Real-Time Architecture</text>
  <text x="30" y="62" fill="#64748b" font-size="13">Production-grade distributed graph execution with state stream and human-in-the-loop triggers</text>

  <!-- Group: Client -->
  <g transform="translate(40, 150)" filter="url(#shadow)">
    <rect width="180" height="360" rx="10" fill="#1e293b" stroke="url(#client-grad)" stroke-width="2" />
    <rect width="180" height="45" rx="10" fill="#0f172a" opacity="0.8" />
    <text x="90" y="28" fill="#f8fafc" font-size="14" font-weight="bold" text-anchor="middle">Client (SPA/React)</text>
    
    <!-- Inner client items -->
    <rect x="15" y="65" width="150" height="55" rx="6" fill="#0f172a" stroke="#334155" />
    <text x="25" y="85" fill="#e2e8f0" font-size="12" font-weight="bold">WebSocket UI</text>
    <text x="25" y="103" fill="#38bdf8" font-size="10">Streaming output view</text>

    <rect x="15" y="135" width="150" height="55" rx="6" fill="#0f172a" stroke="#334155" />
    <text x="25" y="155" fill="#e2e8f0" font-size="12" font-weight="bold">Graph Progress</text>
    <text x="25" y="173" fill="#38bdf8" font-size="10">Node trace visualization</text>

    <rect x="15" y="205" width="150" height="75" rx="6" fill="#14532d" stroke="#15803d" />
    <text x="25" y="225" fill="#f8fafc" font-size="12" font-weight="bold">HITL Prompt</text>
    <text x="25" y="243" fill="#86efac" font-size="10">Approve/reject panel</text>
    <text x="25" y="261" fill="#86efac" font-size="10">Sends approval payload</text>
    
    <text x="90" y="325" fill="#64748b" font-size="11" text-anchor="middle" font-style="italic">Persistent WS Connection</text>
  </g>

  <!-- Group: API Gateway (WebSocket Server) -->
  <g transform="translate(300, 150)" filter="url(#shadow)">
    <rect width="220" height="360" rx="10" fill="#1e293b" stroke="url(#server-grad)" stroke-width="2" />
    <rect width="220" height="45" rx="10" fill="#0f172a" opacity="0.8" />
    <text x="110" y="28" fill="#f8fafc" font-size="14" font-weight="bold" text-anchor="middle">API Server (FastAPI)</text>
    
    <rect x="15" y="65" width="190" height="60" rx="6" fill="#0f172a" stroke="#475569" />
    <text x="25" y="85" fill="#e2e8f0" font-size="12" font-weight="bold">WS Endpoint Handler</text>
    <text x="25" y="105" fill="#818cf8" font-size="10">Manages client-specific sessions</text>

    <rect x="15" y="140" width="190" height="60" rx="6" fill="#0f172a" stroke="#475569" />
    <text x="25" y="160" fill="#e2e8f0" font-size="12" font-weight="bold">Redis Pub/Sub Consumer</text>
    <text x="25" y="180" fill="#818cf8" font-size="10">Listens for worker events</text>

    <rect x="15" y="215" width="190" height="60" rx="6" fill="#0f172a" stroke="#475569" />
    <text x="25" y="235" fill="#e2e8f0" font-size="12" font-weight="bold">Event Stream router</text>
    <text x="25" y="255" fill="#818cf8" font-size="10">Pipes payload to client socket</text>
    
    <rect x="15" y="290" width="190" height="55" rx="6" fill="#0f172a" stroke="#475569" />
    <text x="25" y="310" fill="#e2e8f0" font-size="12" font-weight="bold">HTTP/WS HITL endpoint</text>
    <text x="25" y="328" fill="#818cf8" font-size="10">Receives inputs &amp; unblocks thread</text>
  </g>

  <!-- Group: Message Bus / State DB (Redis / PostgreSQL) -->
  <g transform="translate(580, 80)" filter="url(#shadow)">
    <!-- Redis Box -->
    <rect width="130" height="150" rx="8" fill="#1e293b" stroke="url(#redis-grad)" stroke-width="2" />
    <rect width="130" height="35" rx="8" fill="#0f172a" opacity="0.8" />
    <text x="65" y="22" fill="#f8fafc" font-size="13" font-weight="bold" text-anchor="middle">Redis</text>
    <text x="15" y="65" fill="#fda4af" font-size="11" font-weight="bold">Pub/Sub Channels</text>
    <text x="15" y="82" fill="#e2e8f0" font-size="10">Worker updates</text>
    <text x="15" y="110" fill="#fda4af" font-size="11" font-weight="bold">In-Flight Lock</text>
    <text x="15" y="127" fill="#e2e8f0" font-size="10">Stampede prevention</text>

    <!-- Postgres Box below -->
    <g transform="translate(0, 180)">
      <rect width="130" height="150" rx="8" fill="#1e293b" stroke="url(#postgres-grad)" stroke-width="2" />
      <rect width="130" height="35" rx="8" fill="#0f172a" opacity="0.8" />
      <text x="65" y="22" fill="#f8fafc" font-size="13" font-weight="bold" text-anchor="middle">PostgreSQL</text>
      <text x="15" y="65" fill="#6ee7b7" font-size="11" font-weight="bold">LangGraph State</text>
      <text x="15" y="82" fill="#e2e8f0" font-size="10">Checkpoint DB</text>
      <text x="15" y="110" fill="#6ee7b7" font-size="11" font-weight="bold">State History</text>
      <text x="15" y="127" fill="#e2e8f0" font-size="10">Resume tokens</text>
    </g>
  </g>

  <!-- Group: Agent Worker (LangGraph Executor) -->
  <g transform="translate(770, 240)" filter="url(#shadow)">
    <rect width="150" height="360" rx="10" fill="#1e293b" stroke="url(#worker-grad)" stroke-width="2" />
    <rect width="150" height="45" rx="10" fill="#0f172a" opacity="0.8" />
    <text x="75" y="28" fill="#f8fafc" font-size="13" font-weight="bold" text-anchor="middle">LangGraph Worker</text>
    
    <!-- State machine diagram inside -->
    <rect x="15" y="65" width="120" height="50" rx="6" fill="#881337" stroke="#b91c1c" />
    <text x="75" y="87" fill="#f8fafc" font-size="11" font-weight="bold" text-anchor="middle">Router Node</text>
    <text x="75" y="102" fill="#fca5a5" font-size="9" text-anchor="middle">State: Init</text>

    <rect x="15" y="130" width="120" height="50" rx="6" fill="#881337" stroke="#b91c1c" />
    <text x="75" y="152" fill="#f8fafc" font-size="11" font-weight="bold" text-anchor="middle">Tool Node</text>
    <text x="75" y="167" fill="#fca5a5" font-size="9" text-anchor="middle">State: Call API</text>

    <!-- Interrupt state -->
    <rect x="15" y="195" width="120" height="55" rx="6" fill="#78350f" stroke="#d97706" />
    <text x="75" y="215" fill="#f8fafc" font-size="11" font-weight="bold" text-anchor="middle">Interrupt State</text>
    <text x="75" y="230" fill="#fcd34d" font-size="9" text-anchor="middle">Wait for User Approval</text>

    <rect x="15" y="265" width="120" height="50" rx="6" fill="#881337" stroke="#b91c1c" />
    <text x="75" y="287" fill="#f8fafc" font-size="11" font-weight="bold" text-anchor="middle">Format Node</text>
    <text x="75" y="302" fill="#fca5a5" font-size="9" text-anchor="middle">State: Finalize</text>

    <text x="75" y="342" fill="#64748b" font-size="10" text-anchor="middle">Durable checkpointer</text>
  </g>

  <!-- External LLM Provider -->
  <g transform="translate(580, 470)" filter="url(#shadow)">
    <rect width="130" height="130" rx="8" fill="#1e293b" stroke="url(#llm-grad)" stroke-width="2" />
    <rect width="130" height="35" rx="8" fill="#0f172a" opacity="0.8" />
    <text x="65" y="22" fill="#f8fafc" font-size="13" font-weight="bold" text-anchor="middle">LLM API</text>
    <text x="65" y="65" fill="#d8b4fe" font-size="12" text-anchor="middle" font-weight="bold">Anthropic /</text>
    <text x="65" y="82" fill="#d8b4fe" font-size="12" text-anchor="middle" font-weight="bold">OpenAI</text>
    <text x="65" y="105" fill="#94a3b8" font-size="9" text-anchor="middle">Streaming Tokens</text>
  </g>

  <!-- Connections and Flows -->
  <!-- 1. Client to/from Server WebSocket (Bi-directional) -->
  <path d="M 220 220 L 300 220" stroke="#38bdf8" stroke-width="3" marker-end="url(#arrow-blue)" marker-start="url(#arrow-blue)" />
  <text x="260" y="205" fill="#38bdf8" font-size="11" text-anchor="middle" font-weight="bold">1. WS</text>
  <text x="260" y="238" fill="#64748b" font-size="9" text-anchor="middle">Tokens/State</text>

  <!-- 2. Client approval path -->
  <path d="M 220 375 L 300 440" stroke="#34d399" stroke-width="2" marker-end="url(#arrow)" />
  <text x="260" y="420" fill="#34d399" font-size="10" text-anchor="middle" font-weight="bold">Approval</text>

  <!-- 3. Server to Worker Trigger -->
  <path d="M 520 250 L 770 250" stroke="#818cf8" stroke-width="2" stroke-dasharray="4" marker-end="url(#arrow)" />
  <text x="645" y="240" fill="#818cf8" font-size="10" text-anchor="middle">2. Invoke Graph Run</text>

  <!-- 4. Worker streaming updates to Redis Pub/Sub -->
  <path d="M 770 340 L 710 210" stroke="#fb7185" stroke-width="2" marker-end="url(#arrow-rose)" />
  <text x="760" y="195" fill="#fb7185" font-size="10" text-anchor="middle" font-weight="bold">3. Stream Events</text>

  <!-- 5. Redis Pub/Sub back to Server -->
  <path d="M 580 180 L 450 215" stroke="#f43f5e" stroke-width="2" marker-end="url(#arrow)" />
  <text x="500" y="175" fill="#f43f5e" font-size="10" text-anchor="middle">4. Pub/Sub route</text>

  <!-- 6. Worker Checkpoint read/write Postgres -->
  <path d="M 770 410 L 710 410" stroke="#34d399" stroke-width="2.5" marker-end="url(#arrow)" marker-start="url(#arrow)" />
  <text x="740" y="432" fill="#34d399" font-size="9" text-anchor="middle">Checkpoint</text>

  <!-- 7. Worker call LLM -->
  <path d="M 790 600 L 700 570" stroke="#a855f7" stroke-width="2" marker-end="url(#arrow-purple)" marker-start="url(#arrow-purple)" />
  <text x="765" y="565" fill="#a855f7" font-size="10" text-anchor="middle" font-weight="bold">LLM Call</text>

  <!-- 8. HITL Unblock from Server to Worker (via Postgres state update / resume trigger) -->
  <path d="M 470 350 L 580 390" stroke="#fb7185" stroke-width="2" marker-end="url(#arrow)" />
  <text x="530" y="370" fill="#fb7185" font-size="10" text-anchor="middle" font-weight="bold">Resume</text>

</svg>"""

output_dir = "/home/muklis/Documents/exploring/blog/images/diagrams"
os.makedirs(output_dir, exist_ok=True)
output_path = os.path.join(output_dir, "building-real-time-agentic-workflows-langgraph-websockets.svg")

with open(output_path, "w") as f:
    f.write(svg_content)

print(f"Generated SVG at {output_path}")
