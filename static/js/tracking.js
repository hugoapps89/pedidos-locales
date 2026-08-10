document.addEventListener("DOMContentLoaded",()=>{
  const orderId=window.ORDER_ID;
  const steps=["nuevo","preparando","camino","entregado"];
  const labels={
    nuevo:"Pedido recibido",
    preparando:"Tu pedido se está preparando",
    camino:"🛵 Tu pedido está en camino",
    entregado:"✅ Tu pedido fue entregado",
    cancelado:"❌ Tu pedido fue cancelado"
  };
  const statusEl=document.getElementById("liveStatus");
  const title=document.getElementById("trackingTitle");

  function updateUI(pedido){
    const status=pedido.status;
    document.querySelectorAll(".track-step").forEach(el=>{
      const step=el.dataset.step;
      const idx=steps.indexOf(step);
      const current=steps.indexOf(status);
      el.classList.toggle("done", idx>=0 && idx<current);
      el.classList.toggle("current", step===status);
      el.classList.toggle("future", idx>current);
      if(status==="entregado" && step==="entregado") el.classList.add("current");
    });
    document.querySelectorAll(".track-line").forEach((line,i)=>{
      line.classList.toggle("filled", i < steps.indexOf(status));
    });
    statusEl.textContent=labels[status]||"Actualizando estado...";
    if(title) title.textContent=status==="entregado" ? "¡Pedido entregado!" : status==="camino" ? "¡Tu pedido va en camino!" : status==="preparando" ? "Estamos preparando tu pedido" : "¡Pedido recibido!";
    if(status==="cancelado"){
      statusEl.textContent=labels.cancelado;
      document.querySelectorAll(".track-step").forEach(el=>el.classList.remove("current"));
    }
    if(status==="entregado"){
      document.querySelector(".live-dot")?.classList.add("finished");
    }
  }

  async function refresh(){
    try{
      const r=await fetch(`/pedido/${orderId}/estado?ts=${Date.now()}`,{cache:"no-store"});
      const data=await r.json();
      if(data.ok) updateUI(data.pedido);
    }catch(e){
      statusEl.textContent="No se pudo actualizar. Reintentando...";
    }
  }

  refresh();
  setInterval(refresh,5000);
});