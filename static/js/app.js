document.addEventListener("DOMContentLoaded", () => {

  const menuToggle=document.getElementById("menuToggle");
  const mobileMenu=document.getElementById("mobileMenu");
  const closeMenu=()=>{
    if(!mobileMenu)return;
    mobileMenu.classList.remove("open");
    mobileMenu.setAttribute("aria-hidden","true");
    if(menuToggle)menuToggle.setAttribute("aria-expanded","false");
  };
  if(menuToggle && mobileMenu){
    menuToggle.addEventListener("click",()=>{
      const open=!mobileMenu.classList.contains("open");
      mobileMenu.classList.toggle("open",open);
      mobileMenu.setAttribute("aria-hidden",open?"false":"true");
      menuToggle.setAttribute("aria-expanded",open?"true":"false");
    });
    mobileMenu.querySelectorAll("[data-menu-close]").forEach(el=>el.addEventListener("click",closeMenu));
    mobileMenu.querySelectorAll("a").forEach(a=>a.addEventListener("click",closeMenu));
    document.addEventListener("keydown",e=>{if(e.key==="Escape")closeMenu();});
  }
  const cart = [];
  const countEls = [document.getElementById("cartCount"), document.getElementById("cartBadge")].filter(Boolean);
  const itemsEl = document.getElementById("cartItems");
  const totalEl = document.getElementById("cartTotal");
  const subtotalEl = document.getElementById("cartSubtotal");
  const checkoutSubtotal = document.getElementById("checkoutSubtotal");
  const checkoutBtn = document.getElementById("checkoutBtn");
  const money = n => "$" + Number(n).toFixed(2);

  const config = window.NEGOCIO || {};
  const deliveryFee = config.deliveryEnabled ? Number(config.deliveryFee || 0) : 0;
  const commissionRate = config.commissionEnabled ? Number(config.commissionRate || 0) : 0;

  function subtotal() { return cart.reduce((s,x)=>s+x.price*x.qty,0); }
  function commission() { return subtotal() * commissionRate / 100; }
  function grandTotal() { return subtotal() + deliveryFee + commission(); }

  function renderCart() {
    const qty = cart.reduce((s,x)=>s+x.qty,0);
    countEls.forEach(el=>el.textContent=qty);
    const sub=subtotal(), com=commission(), total=grandTotal();
    if(subtotalEl) subtotalEl.textContent=money(sub);
    const commissionEl=document.getElementById("commissionDisplay");
    if(commissionEl) commissionEl.textContent=money(com);
    const checkoutCommission=document.getElementById("checkoutCommission");
    if(checkoutCommission) checkoutCommission.textContent=money(com);
    if(totalEl) totalEl.textContent=money(total);
    if(checkoutBtn) checkoutBtn.disabled=cart.length===0;
    if(!itemsEl) return;
    if(!cart.length){itemsEl.innerHTML='<p class="empty">Aún no agregas productos.</p>';return;}
    itemsEl.innerHTML=cart.map(x=>`
      <div class="cart-item">
        <div><b>${x.name}</b><small>${money(x.price)} · ${x.qty} pieza(s)</small></div>
        <div class="qty">
          <button type="button" data-action="minus" data-id="${x.id}">−</button>
          <button type="button" data-action="plus" data-id="${x.id}">+</button>
        </div>
      </div>`).join("");
  }

  document.querySelectorAll(".add-product").forEach(btn=>{
    btn.addEventListener("click",()=>{
      const id=Number(btn.dataset.id), x=cart.find(i=>i.id===id);
      if(x)x.qty++; else cart.push({id,name:btn.dataset.name,price:Number(btn.dataset.price),qty:1});
      renderCart();
    });
  });

  if(itemsEl) itemsEl.addEventListener("click",e=>{
    const b=e.target.closest("button[data-id]"); if(!b)return;
    const x=cart.find(i=>i.id===Number(b.dataset.id)); if(!x)return;
    x.qty += b.dataset.action==="plus" ? 1 : -1;
    if(x.qty<=0) cart.splice(cart.indexOf(x),1);
    renderCart();
  });

  const search=document.getElementById("searchInput"), result=document.getElementById("resultText");
  if(search) search.addEventListener("input",()=>{
    const q=search.value.toLowerCase().trim(); let shown=0;
    document.querySelectorAll(".business-card").forEach(card=>{
      const ok=card.dataset.search.toLowerCase().includes(q);
      card.style.display=ok?"":"none"; if(ok)shown++;
    });
    if(result)result.textContent=q?`${shown} resultado(s)`:"";
  });

  const modal=document.getElementById("checkoutModal"), close=document.getElementById("closeCheckout");
  const form=document.getElementById("checkoutForm"), err=document.getElementById("checkoutError"), checkoutTotal=document.getElementById("checkoutTotal");

  if(checkoutBtn && modal && form){
    checkoutBtn.addEventListener("click",()=>{
      if(!cart.length)return;
      const sub=subtotal(), com=commission();
      if(checkoutSubtotal) checkoutSubtotal.textContent=money(sub);
      const checkoutDeliveryFee=document.getElementById("checkoutDeliveryFee");
      if(checkoutDeliveryFee) checkoutDeliveryFee.textContent=money(deliveryFee);
      const checkoutCommission=document.getElementById("checkoutCommission");
      if(checkoutCommission) checkoutCommission.textContent=money(com);
      checkoutTotal.textContent=money(grandTotal());
      modal.classList.remove("hidden");
    });
    close.addEventListener("click",()=>modal.classList.add("hidden"));
    modal.addEventListener("click",e=>{if(e.target===modal)modal.classList.add("hidden")});

    form.addEventListener("submit",async e=>{
      e.preventDefault(); err.textContent="";
      const fd=new FormData(form);
      const payload={
        business_id:window.NEGOCIO.id,
        customer:{name:fd.get("name"),phone:fd.get("phone"),address:fd.get("address"),payment_method:fd.get("payment_method"),notes:fd.get("notes")},
        items:cart.map(x=>({id:x.id,qty:x.qty}))
      };
      try{
        const r=await fetch("/pedido/crear",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
        const data=await r.json();
        if(!data.ok){err.textContent=data.error||"No se pudo registrar el pedido.";return;}
        window.location.href="/pedido/"+data.order_id+"/confirmado";
      }catch(ex){err.textContent="No se pudo conectar con el servidor.";}
    });
  }
  renderCart();
});
