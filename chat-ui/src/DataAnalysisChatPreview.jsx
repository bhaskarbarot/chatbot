import { useState, useEffect, useRef } from "react";

const DEFAULT_API = "http://localhost:8000";
const EXAMPLES = [
  "How many deals are there?",
  "Give me all closed won deals",
  "Show me total revenue by status",
  "Search for Ketul",
  "What can you give me from the data?",
];

let _id = 0;
const uid = () => `msg_${++_id}_${Date.now()}`;

function renderMd(t) {
  if (!t) return "";
  return t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/`([^`]+)`/g,'<code style="background:rgba(0,0,0,.06);padding:1px 5px;border-radius:4px;font-size:.88em">$1</code>')
    .replace(/\*\*(.+?)\*\*/g,"<strong>$1</strong>").replace(/\*(.+?)\*/g,"<em>$1</em>")
    .replace(/\n/g,"<br/>");
}

function Badge({children,color="blue"}){
  const m={blue:{bg:"rgba(37,99,235,.08)",b:"rgba(37,99,235,.18)",c:"#2563eb"},amber:{bg:"rgba(217,119,6,.08)",b:"rgba(217,119,6,.18)",c:"#b45309"},green:{bg:"rgba(22,163,74,.08)",b:"rgba(22,163,74,.18)",c:"#15803d"},purple:{bg:"rgba(124,58,237,.08)",b:"rgba(124,58,237,.18)",c:"#7c3aed"},slate:{bg:"rgba(100,116,139,.08)",b:"rgba(100,116,139,.18)",c:"#475569"}};
  const s=m[color]||m.blue;
  return <span style={{display:"inline-block",padding:"2px 10px",borderRadius:20,fontSize:".74em",fontWeight:600,background:s.bg,border:`1px solid ${s.b}`,color:s.c,margin:"2px 4px 2px 0",lineHeight:1.7}}>{children}</span>;
}

function MetricCard({label,value}){
  return <div style={{flex:"1 1 120px",background:"var(--sr)",border:"1px solid var(--bd)",borderRadius:12,padding:"12px 14px",textAlign:"center",minWidth:100}}>
    <div style={{fontSize:"1.3em",fontWeight:700,color:"var(--ac)"}}>{value}</div>
    <div style={{fontSize:".76em",color:"var(--tm)",marginTop:2,textTransform:"capitalize"}}>{label.replace(/_/g," ")}</div>
  </div>;
}

function DataTable({rows}){
  if(!rows?.length)return null;
  const keys=Object.keys(rows[0]);
  return <div style={{overflowX:"auto",margin:"6px 0",borderRadius:10,border:"1px solid var(--bd)"}}>
    <table style={{width:"100%",borderCollapse:"collapse",fontSize:".82em"}}>
      <thead><tr style={{background:"var(--sr)"}}>
        {keys.map(k=><th key={k} style={{padding:"7px 10px",textAlign:"left",fontWeight:600,color:"var(--ts)",borderBottom:"2px solid var(--bd)",textTransform:"capitalize",whiteSpace:"nowrap"}}>{k.replace(/_/g," ")}</th>)}
      </tr></thead>
      <tbody>{rows.map((row,i)=><tr key={i} style={{background:i%2?"var(--sr)":"transparent"}}>
        {keys.map(k=><td key={k} style={{padding:"6px 10px",borderBottom:"1px solid var(--bd)",color:"var(--tp)",maxWidth:220,overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>{typeof row[k]==="object"?JSON.stringify(row[k]):String(row[k]??"")}</td>)}
      </tr>)}</tbody>
    </table>
  </div>;
}

function Collapsible({title,children,defaultOpen=false}){
  const[open,setOpen]=useState(defaultOpen);
  return <div style={{margin:"5px 0",borderRadius:10,border:"1px solid var(--bd)",overflow:"hidden"}}>
    <button onClick={()=>setOpen(!open)} style={{width:"100%",display:"flex",alignItems:"center",justifyContent:"space-between",padding:"7px 12px",background:"var(--sr)",border:"none",cursor:"pointer",fontSize:".8em",fontWeight:600,color:"var(--ts)",fontFamily:"inherit"}}>
      <span>{title}</span><span style={{transform:open?"rotate(180deg)":"none",transition:"transform .2s"}}>▾</span>
    </button>
    {open&&<div style={{padding:"8px 12px",fontSize:".82em"}}>{children}</div>}
  </div>;
}

function StructuredData({data}){
  if(!data)return null;const parts=[];
  if(Array.isArray(data)&&data.length&&typeof data[0]==="object")parts.push(<DataTable key="r" rows={data}/>);
  if(data&&typeof data==="object"&&!Array.isArray(data)){
    if(data.metrics&&typeof data.metrics==="object"&&Object.keys(data.metrics).length)
      parts.push(<div key="m" style={{display:"flex",flexWrap:"wrap",gap:6,margin:"6px 0"}}>{Object.entries(data.metrics).map(([k,v])=><MetricCard key={k} label={k} value={v}/>)}</div>);
    if(Array.isArray(data.insights)&&data.insights.length)
      parts.push(<div key="i">{data.insights.map((ins,i)=><div key={i} style={{padding:"7px 12px",margin:"3px 0",borderRadius:8,background:"rgba(37,99,235,.06)",borderLeft:"3px solid var(--ac)",fontSize:".86em",color:"var(--tp)"}}>{ins}</div>)}</div>);
    // Handle data.results.{collectionName: [...]} shape from backend
    if(data.results&&typeof data.results==="object"&&!Array.isArray(data.results)){
      Object.entries(data.results).forEach(([coll,rows])=>{
        if(Array.isArray(rows)&&rows.length&&typeof rows[0]==="object")
          parts.push(<div key={`r_${coll}`}><div style={{fontSize:".8em",fontWeight:600,color:"var(--ts)",margin:"6px 0 3px",textTransform:"capitalize"}}>{coll.replace(/_/g," ")} ({rows.length})</div><DataTable rows={rows}/></div>);
      });
    }
    // Handle flat array fields
    Object.entries(data).forEach(([k,v])=>{
      if(["metrics","insights","results","queries_used","collections_used","total_count"].includes(k))return;
      if(Array.isArray(v)&&v.length&&typeof v[0]==="object")
        parts.push(<div key={k}><div style={{fontSize:".8em",fontWeight:600,color:"var(--ts)",margin:"6px 0 3px",textTransform:"capitalize"}}>{k.replace(/_/g," ")}</div><DataTable rows={v}/></div>);
    });
  }
  if(!parts.length)return null;
  return <div style={{marginTop:6}}>{parts}</div>;
}

function QueryPlan({plan}){
  if(!plan||typeof plan!=="object")return null;const items=[];
  if(plan.intent)items.push({l:`Intent: ${plan.intent}`,c:"amber"});
  if(plan.collection)items.push({l:`Collection: ${plan.collection}`,c:"blue"});
  if(plan.filters&&Object.keys(plan.filters).length)items.push({l:`Filters: ${Object.entries(plan.filters).map(([k,v])=>`${k}=${v}`).join(", ")}`,c:"purple"});
  if(plan.output_format)items.push({l:`Output: ${plan.output_format}`,c:"green"});
  if(!items.length)return null;
  return <div style={{margin:"3px 0 5px"}}>{items.map((it,i)=><Badge key={i} color={it.c}>{it.l}</Badge>)}</div>;
}

function FeedbackWidget({onSubmit}){
  const[open,setOpen]=useState(false);const[done,setDone]=useState(false);
  const[rating,setRating]=useState(0);const[hover,setHover]=useState(0);const[comment,setComment]=useState("");
  if(done)return <div style={{fontSize:".78em",color:"#16a34a",margin:"5px 0",display:"flex",alignItems:"center",gap:4}}>
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#16a34a" strokeWidth="2.5"><path d="M20 6 9 17l-5-5"/></svg>
    Feedback submitted!
  </div>;
  return <div style={{margin:"5px 0"}}>
    <button onClick={()=>setOpen(!open)} style={{background:"none",border:"none",cursor:"pointer",padding:0,fontSize:".76em",color:"var(--tm)",display:"flex",alignItems:"center",gap:4}}>
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>
      Rate response <span style={{transform:open?"rotate(180deg)":"none",transition:"transform .2s",fontSize:".9em"}}>▾</span>
    </button>
    {open&&<div style={{marginTop:6,padding:"12px 14px",borderRadius:12,background:"var(--sr)",border:"1px solid var(--bd)"}}>
      <div style={{display:"flex",gap:3,marginBottom:8}}>
        {[1,2,3,4,5].map(s=><button key={s} onMouseEnter={()=>setHover(s)} onMouseLeave={()=>setHover(0)} onClick={()=>setRating(s)}
          style={{background:"none",border:"none",cursor:"pointer",padding:2,fontSize:"1.2em",color:s<=(hover||rating)?"#f59e0b":"var(--bd)",transition:"all .15s",transform:s<=(hover||rating)?"scale(1.15)":"scale(1)"}}>★</button>)}
        {rating>0&&<span style={{fontSize:".76em",color:"var(--tm)",alignSelf:"center",marginLeft:5}}>{["","Poor","Fair","OK","Good","Excellent"][rating]}</span>}
      </div>
      <textarea value={comment} onChange={e=>setComment(e.target.value)} placeholder="What to improve…" rows={2}
        style={{width:"100%",resize:"vertical",borderRadius:8,border:"1px solid var(--bd)",padding:"7px 9px",fontSize:".82em",fontFamily:"inherit",background:"var(--sf)",color:"var(--tp)",outline:"none"}}/>
      <button onClick={()=>{if(rating){setDone(true);onSubmit?.({rating,comment});}}} disabled={!rating}
        style={{marginTop:6,width:"100%",padding:"7px 0",borderRadius:8,border:"none",background:rating?"var(--ac)":"var(--bd)",color:"#fff",fontWeight:600,fontSize:".82em",cursor:rating?"pointer":"default",fontFamily:"inherit"}}>Submit</button>
    </div>}
  </div>;
}

function MessageBubble({msg}){
  const u=msg.role==="user";
  return <div style={{display:"flex",justifyContent:u?"flex-end":"flex-start",marginBottom:5,animation:"fadeUp .3s ease"}}>
    <div style={{maxWidth:u?"72%":"90%",minWidth:0}}>
      <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:3,flexDirection:u?"row-reverse":"row"}}>
        <div style={{width:24,height:24,borderRadius:"50%",display:"flex",alignItems:"center",justifyContent:"center",fontSize:".68em",fontWeight:700,flexShrink:0,background:u?"var(--ac)":"linear-gradient(135deg,#e0c3fc 0%,#8ec5fc 100%)",color:u?"#fff":"#1e293b"}}>{u?"U":"AI"}</div>
        <span style={{fontSize:".72em",fontWeight:600,color:"var(--tm)"}}>{u?"You":"Data Agent"}</span>
      </div>
      <div style={{padding:"11px 15px",borderRadius:u?"16px 16px 4px 16px":"16px 16px 16px 4px",background:u?"var(--ac)":"var(--sr)",color:u?"#fff":"var(--tp)",border:u?"none":"1px solid var(--bd)",fontSize:".9em",lineHeight:1.55,wordBreak:"break-word"}}>
        <span dangerouslySetInnerHTML={{__html:renderMd(msg.content)}}/>
      </div>
      {!u&&<div style={{marginTop:3,paddingLeft:3}}>
        <QueryPlan plan={msg.query_plan}/>
        <StructuredData data={msg.data}/>
        {msg.query_used&&<Collapsible title="Query Used (MongoDB)">{typeof msg.query_used==="object"?Object.entries(msg.query_used).map(([c,s])=><div key={c}><div style={{fontWeight:600,fontSize:".8em",color:"var(--ac)",marginBottom:3}}>Collection: {c}</div><pre style={{background:"var(--sr)",borderRadius:6,padding:8,fontSize:".78em",overflow:"auto",maxHeight:200,margin:0,color:"var(--ts)"}}>{JSON.stringify(s,null,2)}</pre></div>):<pre style={{background:"var(--sr)",borderRadius:6,padding:8,fontSize:".78em",overflow:"auto",color:"var(--ts)"}}>{JSON.stringify(msg.query_used,null,2)}</pre>}</Collapsible>}
        {msg.data&&<Collapsible title="Raw JSON"><pre style={{background:"var(--sr)",borderRadius:6,padding:8,fontSize:".78em",overflow:"auto",maxHeight:240,margin:0,color:"var(--ts)"}}>{JSON.stringify(msg.data,null,2)}</pre></Collapsible>}
        {msg.sources_used?.length>0&&<div style={{marginTop:3}}>{msg.sources_used.map((s,i)=><Badge key={i} color="slate">{s}</Badge>)}</div>}
        {(msg.confidence!=null||msg.processing_time_ms!=null)&&<div style={{fontSize:".73em",color:"var(--tm)",marginTop:3,display:"flex",gap:10}}>
          {msg.confidence!=null&&<span>Confidence: {Math.round(msg.confidence*100)}%</span>}
          {msg.processing_time_ms!=null&&<span>Time: {Math.round(msg.processing_time_ms)}ms</span>}
          {msg.agent_time_ms!=null&&<span>Agent: {Math.round(msg.agent_time_ms)}ms</span>}
        </div>}
        <FeedbackWidget onSubmit={msg.onFeedback}/>
      </div>}
    </div>
  </div>;
}

const THINKING_STEPS = [
  "Reading your query...",
  "Understanding the intent...",
  "Classifying query type...",
  "Identifying relevant data source...",
  "Scanning MongoDB collections...",
  "Matching collection schema...",
  "Checking available fields...",
  "Building database filter...",
  "Routing to the right collection...",
  "Running MongoDB query...",
  "Fetching matching documents...",
  "Counting results...",
  "Applying business rules...",
  "Filtering sensitive fields...",
  "Masking internal IDs...",
  "Formatting data for display...",
  "Generating response structure...",
  "Applying date formatting...",
  "Running safety checks...",
  "Preparing final response...",
  "Validating output quality...",
  "Almost there...",
  "Finalizing your answer...",
];

function TypingDots(){
  const [stepIdx, setStepIdx] = useState(0);
  const [dots, setDots] = useState("");

  // Rotate through the pipeline steps in order every 1.8s.
  // We keep the sequence predictable so the user sees a clear,
  // end-to-end pipeline progression instead of random jumps.
  useEffect(()=>{
    const stepTimer = setInterval(()=>{
      setStepIdx(prev => (prev + 1) % THINKING_STEPS.length);
    }, 1800);
    const dotTimer = setInterval(()=>{
      setDots(d => d.length >= 3 ? "" : d + ".");
    }, 400);
    return ()=>{ clearInterval(stepTimer); clearInterval(dotTimer); };
  },[]);

  return <div style={{display:"flex",justifyContent:"flex-start",marginBottom:5}}>
    <div><div style={{display:"flex",alignItems:"center",gap:6,marginBottom:3}}>
      <div style={{width:24,height:24,borderRadius:"50%",display:"flex",alignItems:"center",justifyContent:"center",fontSize:".68em",fontWeight:700,background:"linear-gradient(135deg,#e0c3fc 0%,#8ec5fc 100%)",color:"#1e293b"}}>AI</div>
      <span style={{fontSize:".72em",fontWeight:600,color:"var(--tm)"}}>Data Agent</span>
    </div>
    <div style={{padding:"12px 18px",borderRadius:"16px 16px 16px 4px",background:"var(--sr)",border:"1px solid var(--bd)",minWidth:220}}>
      <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:6}}>
        {[0,1,2].map(i=><span key={i} style={{width:6,height:6,borderRadius:"50%",background:"var(--ac)",opacity:.7,animation:`pulse 1.2s ease-in-out ${i*.15}s infinite`}}/>)}
      </div>
      <div style={{fontSize:".82em",color:"var(--ts)",fontWeight:500,minHeight:"1.2em",transition:"opacity .3s"}}>
        {THINKING_STEPS[stepIdx]}<span style={{color:"var(--ac)",fontWeight:700}}>{dots}</span>
      </div>
      <div style={{marginTop:6,height:2,borderRadius:2,background:"var(--bd)",overflow:"hidden"}}>
        <div key={stepIdx} style={{height:"100%",borderRadius:2,background:"linear-gradient(90deg,var(--ac),#8ec5fc)",animation:"progressBar 1.8s ease-in-out"}}/>
      </div>
    </div></div>
  </div>;
}

function EmptyState({onExample}){
  return <div style={{flex:1,display:"flex",flexDirection:"column",alignItems:"center",justifyContent:"center",padding:32,textAlign:"center",animation:"fadeUp .5s ease"}}>
    <div style={{width:56,height:56,borderRadius:18,marginBottom:16,background:"linear-gradient(135deg,#e0c3fc 0%,#8ec5fc 100%)",display:"flex",alignItems:"center",justifyContent:"center",fontSize:"1.4em",boxShadow:"0 8px 28px rgba(142,197,252,.25)"}}>⚡</div>
    <h2 style={{margin:0,fontWeight:700,color:"var(--tp)",fontSize:"1.2em"}}>AI Data Analysis Agent</h2>
    <p style={{color:"var(--tm)",fontSize:".88em",maxWidth:360,lineHeight:1.5,margin:"6px 0 20px"}}>Ask questions about your data in natural language</p>
    <div style={{display:"flex",flexWrap:"wrap",gap:7,justifyContent:"center",maxWidth:460}}>
      {EXAMPLES.slice(0,4).map((ex,i)=><button key={i} onClick={()=>onExample(ex)}
        style={{padding:"7px 14px",borderRadius:18,border:"1px solid var(--bd)",background:"var(--sr)",color:"var(--tp)",fontSize:".82em",cursor:"pointer",fontFamily:"inherit",transition:"all .15s"}}
        onMouseEnter={e=>{e.currentTarget.style.background="var(--ac)";e.currentTarget.style.color="#fff";e.currentTarget.style.borderColor="var(--ac)";}}
        onMouseLeave={e=>{e.currentTarget.style.background="var(--sr)";e.currentTarget.style.color="var(--tp)";e.currentTarget.style.borderColor="var(--bd)";}}
      >{ex}</button>)}
    </div>
  </div>;
}

function Sidebar({open,onClose,apiUrl,setApiUrl,status,onClearChat,onExample}){
  return <>
    {open&&<div onClick={onClose} style={{position:"fixed",inset:0,background:"rgba(0,0,0,.2)",zIndex:90,animation:"fadeIn .2s"}}/>}
    <aside style={{position:"fixed",top:0,left:open?0:-320,width:300,height:"100%",background:"var(--sf)",borderRight:"1px solid var(--bd)",zIndex:100,transition:"left .28s cubic-bezier(.4,0,.2,1)",display:"flex",flexDirection:"column",overflowY:"auto",boxShadow:open?"4px 0 20px rgba(0,0,0,.06)":"none"}}>
      <div style={{padding:"16px 16px 0"}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"center"}}>
          <span style={{fontWeight:700,fontSize:"1em",color:"var(--tp)"}}>Settings</span>
          <button onClick={onClose} style={{background:"none",border:"none",cursor:"pointer",fontSize:"1.1em",color:"var(--tm)",padding:3}}>✕</button>
        </div>
        <label style={{fontSize:".76em",fontWeight:600,color:"var(--ts)",marginTop:14,display:"block"}}>API URL</label>
        <input value={apiUrl} onChange={e=>setApiUrl(e.target.value)} style={{width:"100%",padding:"7px 9px",borderRadius:8,border:"1px solid var(--bd)",fontSize:".84em",marginTop:3,background:"var(--sr)",color:"var(--tp)",outline:"none",fontFamily:"inherit"}}/>
        <div style={{marginTop:8,padding:"5px 10px",borderRadius:8,fontSize:".8em",fontWeight:600,background:status?"rgba(22,163,74,.08)":"rgba(220,38,38,.08)",color:status?"#16a34a":"#dc2626",border:`1px solid ${status?"rgba(22,163,74,.18)":"rgba(220,38,38,.18)"}`}}>
          {status?"● Online":"● Offline (demo mode)"}
        </div>
        <button onClick={onClearChat} style={{marginTop:10,width:"100%",padding:"6px 0",borderRadius:8,border:"1px solid var(--bd)",background:"var(--sr)",color:"var(--ts)",fontSize:".78em",fontWeight:600,cursor:"pointer",fontFamily:"inherit"}}>Clear Chat</button>
      </div>
      <hr style={{border:"none",borderTop:"1px solid var(--bd)",margin:"14px 16px"}}/>
      <div style={{padding:"0 16px 20px"}}>
        <span style={{fontWeight:700,fontSize:".88em",color:"var(--tp)"}}>Try These</span>
        <div style={{marginTop:6,display:"flex",flexDirection:"column",gap:4}}>
          {EXAMPLES.map((ex,i)=><button key={i} onClick={()=>{onExample(ex);onClose();}} style={{textAlign:"left",padding:"7px 10px",borderRadius:8,background:"var(--sr)",border:"1px solid var(--bd)",fontSize:".8em",color:"var(--tp)",cursor:"pointer",fontFamily:"inherit",transition:"background .15s"}}
            onMouseEnter={e=>e.currentTarget.style.background="rgba(37,99,235,.07)"}
            onMouseLeave={e=>e.currentTarget.style.background="var(--sr)"}
          >{ex}</button>)}
        </div>
      </div>
    </aside>
  </>;
}

// ─── DEMO: fake responses when backend is offline ────────────────────────────
const DEMO_RESPONSES = {
  "how many deals are there?": {
    answer: "There are **42 deals** in the database across all statuses.",
    data: { metrics: { total_deals: 42, open: 18, closed_won: 15, closed_lost: 9 } },
    sources_used: ["deals"], confidence: 0.97, processing_time_ms: 127,
    query_plan: { intent: "count", collection: "deals", filters: {}, output_format: "metric" },
  },
  "give me all closed won deals": {
    answer: "Here are all **15 closed won** deals:",
    data: [
      { deal_name: "Acme Corp", value: "$125,000", stage: "Closed Won", owner: "Sarah J." },
      { deal_name: "TechStart Inc", value: "$89,500", stage: "Closed Won", owner: "Mike R." },
      { deal_name: "GlobalFin", value: "$210,000", stage: "Closed Won", owner: "Sarah J." },
      { deal_name: "DataFlow", value: "$67,200", stage: "Closed Won", owner: "Alex K." },
      { deal_name: "CloudNine", value: "$155,000", stage: "Closed Won", owner: "Mike R." },
    ],
    sources_used: ["deals"], confidence: 0.99, processing_time_ms: 89,
    query_plan: { intent: "list", collection: "deals", filters: { status: "closed_won" }, output_format: "table" },
    query_used: { deals: { filter: { stage: "Closed Won" }, projection: { deal_name:1, value:1, stage:1, owner:1 } } },
  },
  "show me total revenue by status": {
    answer: "Here's the revenue breakdown by deal status:",
    data: {
      metrics: { closed_won: "$1,245,000", open: "$687,300", closed_lost: "$312,800" },
      insights: ["Closed Won represents 55% of total pipeline", "Open deals show strong potential at 30%"],
    },
    sources_used: ["deals"], confidence: 0.94, processing_time_ms: 215,
    query_plan: { intent: "aggregate", collection: "deals", filters: {}, output_format: "metrics" },
  },
  "search for Ketul": {
    answer: "Found **3 records** matching 'John':",
    data: [
      { name: "John Smith", email: "john.s@acme.com", role: "VP Sales", company: "Acme Corp" },
      { name: "John Lee", email: "jlee@techstart.io", role: "CTO", company: "TechStart" },
      { name: "Johnny Walker", email: "jw@globalfin.com", role: "Director", company: "GlobalFin" },
    ],
    sources_used: ["contacts"], confidence: 0.91, processing_time_ms: 156,
    query_plan: { intent: "search", collection: "contacts", filters: { name: "John" }, output_format: "table" },
  },
};

function getDemoResponse(query) {
  const q = query.toLowerCase().trim().replace(/[?!.,]+$/,"");
  for (const [k, v] of Object.entries(DEMO_RESPONSES)) {
    if (q === k || q.includes(k.split(" ").slice(0,3).join(" "))) return v;
  }
  return {
    answer: `I understood your question: **"${query}"**\n\nIn demo mode, I can answer these sample queries. Connect to your backend API to get real results!\n\nTry: *"How many deals are there?"* or *"Show me total revenue by status"*`,
    confidence: 0.5, processing_time_ms: 42,
    query_plan: { intent: "unknown", collection: "—", filters: {} },
  };
}

// ═════════════════════════════════════════════════════════════════════════════
export default function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [apiUrl, setApiUrl] = useState(DEFAULT_API);
  const [backendUp, setBackendUp] = useState(false);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, loading]);

  const sendMessage = async (text) => {
    const trimmed = (text || input).trim();
    if (!trimmed || loading) return;
    const userMsg = { id: uid(), role: "user", content: trimmed };
    setMessages(prev => [...prev, userMsg]);
    setInput("");
    setLoading(true);

    // Try real backend first, fall back to demo
    let resp;
    try {
      const r = await fetch(`${apiUrl.replace(/\/+$/,"")}/chat`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: trimmed }),
        signal: AbortSignal.timeout(30000),
      });
      if (r.ok) { resp = await r.json(); setBackendUp(true); }
      else throw new Error();
    } catch {
      resp = getDemoResponse(trimmed);
      setBackendUp(false);
    }

    const botMsg = {
      id: uid(), role: "assistant",
      content: resp.answer || "(No response)",
      data: resp.data, sources_used: resp.sources_used || [],
      confidence: resp.confidence, processing_time_ms: resp.processing_time_ms,
      agent_time_ms: resp.agent_time_ms, query_used: resp.query_used,
      query_plan: resp.query_plan, _userQuery: trimmed,
    };

    await new Promise(r => setTimeout(r, 600 + Math.random() * 400));
    setMessages(prev => [...prev, botMsg]);
    setLoading(false);
    setTimeout(() => inputRef.current?.focus(), 60);
  };

  return (
    <div style={{
      "--ac":"#2563eb","--as":"rgba(37,99,235,.07)","--sf":"#fafbfc","--sr":"#f1f3f5",
      "--bd":"#e2e5ea","--tp":"#1a1d23","--ts":"#4b5563","--tm":"#9ca3af",
      fontFamily:"'DM Sans','Segoe UI',system-ui,sans-serif",
      height:"100vh",display:"flex",flexDirection:"column",background:"var(--sf)",color:"var(--tp)",
    }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&display=swap');
        @keyframes fadeUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
        @keyframes fadeIn{from{opacity:0}to{opacity:1}}
        @keyframes pulse{0%,80%,100%{opacity:.35;transform:scale(.85)}40%{opacity:1;transform:scale(1)}}
        @keyframes progressBar{0%{width:0%;margin-left:0}60%{width:70%}100%{width:100%;margin-left:0}}
        *{box-sizing:border-box}::-webkit-scrollbar{width:5px}::-webkit-scrollbar-thumb{background:#d1d5db;border-radius:3px}
        textarea:focus,input:focus{border-color:var(--ac)!important;box-shadow:0 0 0 2px rgba(37,99,235,.12)!important}
      `}</style>

      <Sidebar open={sidebarOpen} onClose={()=>setSidebarOpen(false)} apiUrl={apiUrl} setApiUrl={setApiUrl} status={backendUp}
        onClearChat={()=>setMessages([])} onExample={sendMessage}/>

      <header style={{padding:"11px 16px",borderBottom:"1px solid var(--bd)",display:"flex",alignItems:"center",gap:10,background:"#fff",flexShrink:0}}>
        <button onClick={()=>setSidebarOpen(true)} style={{background:"none",border:"none",cursor:"pointer",padding:3,display:"flex",color:"var(--ts)"}}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
        </button>
        <div style={{flex:1}}>
          <span style={{fontWeight:700,fontSize:"1em"}}>Data Agent</span>
          <span style={{marginLeft:8,fontSize:".7em",fontWeight:600,padding:"2px 7px",borderRadius:10,verticalAlign:"middle",background:backendUp?"rgba(22,163,74,.08)":"rgba(217,119,6,.08)",color:backendUp?"#16a34a":"#b45309"}}>{backendUp?"Online":"Demo"}</span>
        </div>
        <button onClick={()=>setMessages([])} style={{background:"none",border:"1px solid var(--bd)",borderRadius:8,padding:"4px 10px",fontSize:".78em",fontWeight:600,cursor:"pointer",color:"var(--ts)",fontFamily:"inherit"}}>New Chat</button>
      </header>

      <div style={{flex:1,overflowY:"auto",padding:"16px 16px 0"}}>
        <div style={{maxWidth:720,margin:"0 auto"}}>
          {messages.length===0&&!loading?<EmptyState onExample={sendMessage}/>:<>
            {messages.map(m=><MessageBubble key={m.id} msg={m}/>)}
            {loading&&<TypingDots/>}
          </>}
          <div ref={bottomRef}/>
        </div>
      </div>

      <div style={{padding:"10px 16px 14px",borderTop:"1px solid var(--bd)",background:"#fff",flexShrink:0}}>
        <div style={{maxWidth:720,margin:"0 auto",display:"flex",gap:8,alignItems:"flex-end"}}>
          <textarea ref={inputRef} value={input} onChange={e=>setInput(e.target.value)}
            onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();sendMessage();}}}
            placeholder="Ask me anything about your data…" rows={1}
            style={{flex:1,resize:"none",borderRadius:14,border:"1px solid var(--bd)",padding:"10px 14px",fontSize:".9em",fontFamily:"inherit",background:"var(--sf)",color:"var(--tp)",outline:"none",minHeight:42,maxHeight:120,lineHeight:1.4}}
            onInput={e=>{e.target.style.height="auto";e.target.style.height=Math.min(e.target.scrollHeight,120)+"px";}}/>
          <button onClick={()=>sendMessage()} disabled={!input.trim()||loading} style={{width:42,height:42,borderRadius:14,border:"none",flexShrink:0,background:input.trim()&&!loading?"var(--ac)":"var(--bd)",color:"#fff",cursor:input.trim()&&!loading?"pointer":"default",display:"flex",alignItems:"center",justifyContent:"center",transition:"background .15s"}}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
          </button>
        </div>
        <div style={{textAlign:"center",fontSize:".68em",color:"var(--tm)",marginTop:5,maxWidth:720,margin:"5px auto 0"}}>
          Enter to send · Shift+Enter for newline · ☰ for settings
        </div>
      </div>
    </div>
  );
}
