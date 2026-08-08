HOME_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Potato 工作台</title>
  <script src="https://cdn.jsdelivr.net/npm/gsap@3.13.0/dist/gsap.min.js"></script>
  <script src="https://unpkg.com/lucide@0.468.0/dist/umd/lucide.min.js"></script>
  <style>
    :root{--ink:#18211d;--muted:#6d7771;--line:#dbe2dd;--paper:#f4f6f4;--white:#fff;--green:#176b4b;--green-soft:#e2f1e9;--coral:#b85542;--coral-soft:#f7e8e3;--blue:#285f87;--blue-soft:#e7eff5;--shadow:0 14px 34px rgba(24,33,29,.08)}
    *{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--paper);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;letter-spacing:0}a{color:inherit;text-decoration:none}button{font:inherit}
    header{height:62px;background:var(--ink);color:white;display:flex;align-items:center;padding:0 max(22px,calc((100vw - 1040px)/2));gap:11px}.mark{width:28px;height:28px;border:1px solid #53605a;display:grid;place-items:center;border-radius:5px;font-weight:800;font-size:12px}.brand{font-size:16px;font-weight:720}.gateway-state{margin-left:auto;color:#c3cec8;font-size:12px;display:flex;align-items:center;gap:7px}.dot{width:7px;height:7px;border-radius:50%;background:#68c69c;box-shadow:0 0 0 3px rgba(104,198,156,.13)}
    main{max-width:1040px;margin:0 auto;padding:54px 24px 72px}.heading{display:flex;align-items:end;justify-content:space-between;margin-bottom:20px}.heading h1{font-size:26px;line-height:1.2;margin:0;font-weight:750}.heading time{color:var(--muted);font-size:12px}
    .launchers{display:grid;grid-template-columns:1fr 1fr;gap:16px}.launcher{min-height:238px;background:var(--white);border:1px solid var(--line);border-radius:7px;padding:24px;display:flex;flex-direction:column;position:relative;overflow:hidden;box-shadow:0 2px 0 rgba(24,33,29,.02);transition:border-color .18s,box-shadow .18s,transform .18s}.launcher:hover{transform:translateY(-2px);box-shadow:var(--shadow)}.launcher.workflow:hover{border-color:#dab6ac}.launcher.calibration:hover{border-color:#a9d0bc}.launcher:focus-visible{outline:3px solid rgba(40,95,135,.22);outline-offset:3px}.topline{display:flex;align-items:center;justify-content:space-between}.app-icon{width:45px;height:45px;border-radius:6px;display:grid;place-items:center;font-weight:800;font-size:13px}.workflow .app-icon{background:var(--coral-soft);color:var(--coral)}.calibration .app-icon{background:var(--green-soft);color:var(--green)}.arrow{width:34px;height:34px;border:1px solid var(--line);border-radius:50%;display:grid;place-items:center;font-size:18px}.launcher h2{font-size:21px;margin:38px 0 5px}.meta{color:var(--muted);font-size:12px}.status-line{margin-top:auto;display:flex;align-items:center;gap:7px;font-size:12px;font-weight:650}.status-line::before{content:"";width:6px;height:6px;border-radius:50%;background:#a9b3ad}.status-line.online::before{background:#4bae7d}.status-line.offline{color:#9b4339}.status-line.offline::before{background:#c45c4e}
    .tools{margin-top:32px;border-top:1px solid var(--line)}.tools-head{display:flex;align-items:center;padding:17px 2px 11px}.tools-head h2{font-size:13px;margin:0}.tools-list{display:grid;grid-template-columns:repeat(3,1fr);background:var(--white);border:1px solid var(--line);border-radius:6px}.tool{padding:15px 17px;border-right:1px solid var(--line);display:flex;align-items:center;gap:11px;min-width:0}.tool:last-child{border:0}.tool:hover{background:#fafbfa}.tool-icon{width:30px;height:30px;border-radius:5px;background:var(--blue-soft);color:var(--blue);display:grid;place-items:center;font-size:10px;font-weight:800;flex:0 0 auto}.tool strong{display:block;font-size:13px}.tool span{display:block;color:var(--muted);font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tool .go{margin-left:auto;color:#8a948e}
    #agents{display:none;margin-top:28px}.agent-row{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}.agent{padding:13px 8px;border-right:1px solid var(--line)}.agent:last-child{border:0}.agent b{display:block;font-size:12px}.agent span{color:var(--muted);font-size:11px}.agent.online span{color:var(--green)}
    @media(max-width:700px){header{padding:0 18px}main{padding:34px 16px 54px}.launchers{grid-template-columns:1fr}.launcher{min-height:205px}.launcher h2{margin-top:28px}.tools-list{grid-template-columns:1fr}.tool{border-right:0;border-bottom:1px solid var(--line)}.tool:last-child{border-bottom:0}.agent-row{grid-template-columns:1fr 1fr}.agent:nth-child(2){border-right:0}.agent:nth-child(-n+2){border-bottom:1px solid var(--line)}.heading time{display:none}}
  </style>
</head>
<body>
  <header><div class="mark">P</div><div class="brand">Potato 工作台</div><div class="gateway-state"><span class="dot"></span><span id="gatewayLabel">Gateway</span></div></header>
  <main>
    <div class="heading"><h1>总入口</h1><time id="clock"></time></div>
    <section class="launchers" aria-label="主要入口">
      <a class="launcher workflow" id="workflowLink" href="http://127.0.0.1:8787/workflows">
        <div class="topline"><div class="app-icon">WF</div><div class="arrow" aria-hidden="true">→</div></div>
        <h2>视频工作流</h2><div class="meta">Potato Hub · 8787</div>
        <div class="status-line" id="hubState">检查中</div>
      </a>
      <a class="launcher calibration" href="/calibrations">
        <div class="topline"><div class="app-icon">CA</div><div class="arrow" aria-hidden="true">→</div></div>
        <h2>Agent 校准</h2><div class="meta">Potato Gateway · 8765</div>
        <div class="status-line online">可用</div>
      </a>
    </section>
    <section class="tools"><div class="tools-head"><h2>工具</h2></div><div class="tools-list">
      <a class="tool" href="/docs"><div class="tool-icon">API</div><div><strong>接口文档</strong><span>Swagger UI</span></div><div class="go">→</div></a>
      <a class="tool" href="/potato-actions-v0.2.4.yaml"><div class="tool-icon">YML</div><div><strong>Actions Schema</strong><span>OpenAPI 3.1</span></div><div class="go">→</div></a>
      <a class="tool" id="hubHome" href="http://127.0.0.1:8787/"><div class="tool-icon">HUB</div><div><strong>Hub 消息台</strong><span>Session 与素材</span></div><div class="go">→</div></a>
    </div></section>
    <section id="agents"><div class="tools-head"><h2>Agents</h2></div><div class="agent-row" id="agentRow"></div></section>
  </main>
  <script>
    const hubOrigin=`${location.protocol}//${location.hostname}:8787`;
    document.querySelector('#workflowLink').href=`${hubOrigin}/workflows`;
    document.querySelector('#hubHome').href=`${hubOrigin}/`;
    document.querySelector('#clock').textContent=new Intl.DateTimeFormat('zh-CN',{month:'long',day:'numeric',weekday:'short'}).format(new Date());
    const token=localStorage.getItem('potatoGatewayToken')||'';
    async function loadStatus(){
      if(!token){document.querySelector('#hubState').textContent='打开工作流';return}
      try{
        const response=await fetch('/api/status',{headers:{Authorization:`Bearer ${token}`}});
        if(!response.ok)throw new Error('status unavailable');
        const data=await response.json();
        const hub=document.querySelector('#hubState');
        hub.textContent=data.potato_hub.status==='online'?'在线':'暂不可用';
        hub.classList.add(data.potato_hub.status==='online'?'online':'offline');
        document.querySelector('#agentRow').innerHTML=data.agents.map(item=>`<div class="agent ${item.status==='online'?'online':''}"><b>${item.display_name}</b><span>${item.status}</span></div>`).join('');
        document.querySelector('#agents').style.display='block';
      }catch{document.querySelector('#hubState').textContent='打开工作流'}
    }
    loadStatus();
    if(window.gsap&&!matchMedia('(prefers-reduced-motion: reduce)').matches){
      gsap.from('.launcher',{y:12,autoAlpha:0,duration:.38,stagger:.07,ease:'power1.out'});
      gsap.from('.tools',{y:8,autoAlpha:0,duration:.3,delay:.16,ease:'power1.out'});
    }
  </script>
</body></html>'''
