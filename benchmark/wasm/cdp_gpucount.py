#!/usr/bin/env python3
# Inject a WebGPU-counting shim (before page scripts) that monkeypatches the
# WebGPU prototypes, and capture per-second + per-frame counts of submits,
# command buffers, render/compute passes, draws, setBindGroup/setPipeline, and
# presented frames (getCurrentTexture). Used to compare render-call structure
# 0.16 vs 0.19 on WebGPU. Usage: cdp_gpucount.py <seconds>
import asyncio, json, os, sys, urllib.request, websockets

PORT = int(os.environ.get("CDP_PORT", "9399"))
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 70.0

SHIM = r"""
(function(){
  if (window.__gpucount_installed) return; window.__gpucount_installed = true;
  var c = {frame:0, submit:0, cmdbuf:0, renderPass:0, computePass:0, draw:0, setBindGroup:0, setPipeline:0, setVertexBuffer:0, mapAsync:0, workDone:0, writeBuffer:0, writeTexture:0};
  window.__maxSample = 1; window.__msaaTexN = 0;
  try{ var ct = GPUDevice.prototype.createTexture; GPUDevice.prototype.createTexture = function(d){ try{ if(d&&d.sampleCount>1){ window.__msaaTexN++; if(d.sampleCount>window.__maxSample) window.__maxSample=d.sampleCount; } }catch(e){} return ct.apply(this, arguments); }; }catch(e){}
  function wrap(proto, m, fn){ if(!proto||!proto[m]) return; var o=proto[m]; proto[m]=function(){ fn.apply(this, arguments); return o.apply(this, arguments); }; }
  try{ wrap(GPUQueue.prototype,'submit', function(a){ c.submit++; if(a&&a.length!==undefined) c.cmdbuf+=a.length; }); }catch(e){}
  try{ wrap(GPUCommandEncoder.prototype,'beginRenderPass', function(){ c.renderPass++; }); }catch(e){}
  try{ wrap(GPUCommandEncoder.prototype,'beginComputePass', function(){ c.computePass++; }); }catch(e){}
  try{ wrap(GPURenderPassEncoder.prototype,'draw', function(){ c.draw++; }); }catch(e){}
  try{ wrap(GPURenderPassEncoder.prototype,'drawIndexed', function(){ c.draw++; }); }catch(e){}
  try{ wrap(GPURenderPassEncoder.prototype,'setBindGroup', function(){ c.setBindGroup++; }); }catch(e){}
  try{ wrap(GPURenderPassEncoder.prototype,'setPipeline', function(){ c.setPipeline++; }); }catch(e){}
  try{ wrap(GPURenderPassEncoder.prototype,'setVertexBuffer', function(){ c.setVertexBuffer++; }); }catch(e){}
  try{ wrap(GPUCanvasContext.prototype,'getCurrentTexture', function(){ c.frame++; }); }catch(e){}
  try{ wrap(GPUBuffer.prototype,'mapAsync', function(){ c.mapAsync++; }); }catch(e){}
  try{ wrap(GPUQueue.prototype,'onSubmittedWorkDone', function(){ c.workDone++; }); }catch(e){}
  try{ wrap(GPUQueue.prototype,'writeBuffer', function(){ c.writeBuffer++; }); }catch(e){}
  try{ wrap(GPUQueue.prototype,'writeTexture', function(){ c.writeTexture++; }); }catch(e){}
  var last = Object.assign({}, c);
  setInterval(function(){
    var d = {}; for (var k in c) d[k] = c[k]-last[k]; last = Object.assign({}, c);
    var f = d.frame || 0;
    var per = {};
    if (f>0){ for (var k in d){ if(k!=='frame') per[k] = +(d[k]/f).toFixed(1); } }
    console.log('GPUCOUNT/s fps='+f+' '+JSON.stringify(d)+' /frame='+JSON.stringify(per)+' maxSample='+window.__maxSample+' msaaTex='+window.__msaaTexN);
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
    # Disable client-side keepalive: under heavy host load chrome's main thread is
    # starved and can't pong in time, dropping the CDP socket mid-warmup. We only
    # need call *counts* (not latency), so no keepalive is fine and load-robust.
    async with websockets.connect(ws_url, max_size=None, ping_interval=None, ping_timeout=None) as ws:
        mid = 0
        async def send(m, p=None):
            nonlocal mid; mid += 1
            await ws.send(json.dumps({"id": mid, "method": m, "params": p or {}}))
        # Let the page load + create the WebGPU device + start rendering, THEN
        # inject the shim via Runtime.evaluate. We patch prototype methods, so the
        # already-running render loop's submit/draw/etc. calls get counted.
        await send("Runtime.enable")
        import time
        await asyncio.sleep(float(os.environ.get("GPUCOUNT_WARMUP", "25")))
        await send("Runtime.evaluate", {"expression": SHIM})
        start = time.time()
        while time.time() - start < SECS:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=SECS))
            except asyncio.TimeoutError:
                break
            if msg.get("method") == "Runtime.consoleAPICalled":
                txt = " ".join(str(a.get("value", "")) for a in msg["params"].get("args", []))
                if "GPUCOUNT/s" in txt:
                    print(f"[{time.time()-start:5.0f}] {txt}", flush=True)
    print("GPUCOUNT_DONE", flush=True)

asyncio.run(main())
