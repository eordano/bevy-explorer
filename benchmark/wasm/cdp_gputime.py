#!/usr/bin/env python3
# Measure REAL GPU busy-time per frame on WebGPU, even when the render loop is
# rAF-capped at 60fps (orbit_wall can't see GPU cost when the app holds 60fps —
# the GPU finishes early and idles to the next vsync). We monkeypatch
# GPUQueue.submit to timestamp performance.now() and chain our own
# onSubmittedWorkDone() fence; the submit->done gap is the GPU's actual execution
# latency for that frame's work. The app is cross-origin-isolated (COEP), so
# performance.now() has ~5us resolution. No rebuild, no chrome flags.
# Usage: cdp_gputime.py <seconds>
import asyncio, json, os, sys, urllib.request, websockets

PORT = int(os.environ.get("CDP_PORT", "9344"))
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
WARMUP = float(os.environ.get("GPUTIME_WARMUP", "45"))

SHIM = r"""
(function(){
  if (window.__gputime_installed) return; window.__gputime_installed = true;
  window.__gpu_s = [];
  var origSubmit = GPUQueue.prototype.submit;
  GPUQueue.prototype.submit = function() {
    var r = origSubmit.apply(this, arguments);
    var t0 = performance.now();
    try {
      this.onSubmittedWorkDone().then(function(){
        window.__gpu_s.push(performance.now() - t0);
      });
    } catch(e) {}
    return r;
  };
  setInterval(function(){
    var s = window.__gpu_s.slice().sort(function(a,b){return a-b;});
    window.__gpu_s = [];
    if (s.length>0){
      var p=function(q){return s[Math.min(s.length-1, Math.floor(s.length*q))];};
      var m=s.reduce(function(a,b){return a+b;},0)/s.length;
      console.log('GPUTIME/s n='+s.length+' p50='+p(0.5).toFixed(3)+' p90='+p(0.9).toFixed(3)+' p99='+p(0.99).toFixed(3)+' mean='+m.toFixed(3)+'ms');
    }
  }, 1000);
})();
"""

async def main():
    ws_url = None
    for _ in range(60):
        try:
            data = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list"))
            pages = [t for t in data if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if pages: ws_url = pages[0]["webSocketDebuggerUrl"]; break
        except Exception: pass
        await asyncio.sleep(1)
    if not ws_url: print("NO_PAGE_TARGET"); return
    # ping disabled: load-robust (chrome may be starved and miss keepalive pongs)
    async with websockets.connect(ws_url, max_size=None, ping_interval=None, ping_timeout=None) as ws:
        mid = 0
        async def send(m, p=None):
            nonlocal mid; mid += 1
            await ws.send(json.dumps({"id": mid, "method": m, "params": p or {}}))
        import time
        await send("Runtime.enable")
        await asyncio.sleep(WARMUP)
        await send("Runtime.evaluate", {"expression": SHIM})
        start = time.time()
        while time.time() - start < SECS:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=SECS))
            except asyncio.TimeoutError:
                break
            if msg.get("method") == "Runtime.consoleAPICalled":
                txt = " ".join(str(a.get("value", "")) for a in msg["params"].get("args", []))
                if "GPUTIME/s" in txt:
                    print(f"[{time.time()-start:5.0f}] {txt}", flush=True)
    print("GPUTIME_DONE", flush=True)

asyncio.run(main())
