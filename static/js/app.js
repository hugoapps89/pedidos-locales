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
let appliedCoupon = null;
let couponDiscount = 0;
  const countEls = [document.getElementById("cartCount"), document.getElementById("cartBadge")].filter(Boolean);
  const itemsEl = document.getElementById("cartItems");
  const totalEl = document.getElementById("cartTotal");
  const subtotalEl = document.getElementById("cartSubtotal");
  const checkoutSubtotal = document.getElementById("checkoutSubtotal");
  const checkoutBtn = document.getElementById("checkoutBtn");
const couponCodeInput =
  document.getElementById("couponCode");

const applyCouponBtn =
  document.getElementById("applyCouponBtn");

const couponMessage =
  document.getElementById("couponMessage");
  const money = n => "$" + Number(n).toFixed(2);

const config = window.NEGOCIO || {};
const commissionRate = config.commissionEnabled ? Number(config.commissionRate || 0) : 0;

const isTortilleria =
  String(config.name || "").toLowerCase().includes("tortill") ||
  String(config.category || "").toLowerCase().includes("tortill");

const minimumOrder = isTortilleria ? 50 : 0;

let deliveryFee = config.deliveryEnabled
  ? Number(config.deliveryFee ?? 35)
  : 0;

let deliveryDistanceKm = 0;
let customerLocation = null;

function subtotal() {
    return cart.reduce(
        (s, x) => s + x.price * x.qty,
        0
    );
}

function discountedSubtotal() {
    return Math.max(
        0,
        subtotal() - couponDiscount
    );
}

function commission() {
    return discountedSubtotal() * commissionRate / 100;
}

function grandTotal() {
    return (
        discountedSubtotal() +
        deliveryFee +
        commission()
    );
}


/* ================================
   CUPÓN
   ================================ */

async function applyCoupon() {

    if (!couponCodeInput || !applyCouponBtn) return;

    const code =
        couponCodeInput.value.trim().toUpperCase();

    if (!code) {
        if (couponMessage) {
            couponMessage.textContent =
                "Escribe un código de cupón.";
            couponMessage.className =
                "coupon-message error";
        }
        return;
    }

    if (!cart.length) {
        if (couponMessage) {
            couponMessage.textContent =
                "Agrega productos antes de aplicar un cupón.";
            couponMessage.className =
                "coupon-message error";
        }
        return;
    }

    applyCouponBtn.disabled = true;
    applyCouponBtn.textContent = "Validando...";

    try {

        const url =
            `/api/cupon/validar` +
            `?code=${encodeURIComponent(code)}` +
            `&business_id=${encodeURIComponent(window.NEGOCIO.id)}` +
            `&subtotal=${encodeURIComponent(subtotal())}`;

        const response = await fetch(url, {
            credentials: "same-origin",
            cache: "no-store"
        });

        const data = await response.json();

        if (!response.ok || !data.ok) {
            throw new Error(
                data.error ||
                "No fue posible aplicar el cupón."
            );
        }

        appliedCoupon = {
            id: data.coupon_id,
            code: data.code,
            discount: Number(data.discount || 0)
        };

        couponDiscount =
            Number(data.discount || 0);

        couponCodeInput.value =
            data.code;

        if (couponMessage) {
            couponMessage.textContent =
                data.message;

            couponMessage.className =
                "coupon-message success";
        }

        renderCart();

    } catch (error) {

        appliedCoupon = null;
        couponDiscount = 0;

        if (couponMessage) {
            couponMessage.textContent =
                error.message ||
                "No fue posible aplicar el cupón.";

            couponMessage.className =
                "coupon-message error";
        }

        renderCart();

    } finally {

        applyCouponBtn.disabled = false;
        applyCouponBtn.textContent = "Aplicar";
    }
}


/* ================================
   BOTÓN APLICAR CUPÓN
   ================================ */

if (applyCouponBtn) {
    applyCouponBtn.addEventListener(
        "click",
        applyCoupon
    );
}

if (couponCodeInput) {
    couponCodeInput.addEventListener(
        "keydown",
        event => {
            if (event.key === "Enter") {
                event.preventDefault();
                applyCoupon();
            }
        }
    );
}


const formatDelivery = n =>
    "$" + Number(n || 0).toFixed(0);


function updateDeliveryUI() {

    const d = config.deliveryEnabled
        ? (
            deliveryDistanceKm
                ? `${deliveryDistanceKm.toFixed(1)} km`
                : "calculando…"
        )
        : "";

    const row =
        document.getElementById("deliveryDistanceDisplay");

    const checkoutD =
        document.getElementById(
            "checkoutDeliveryDistance"
        );

    if (row) {
        row.textContent =
            d ? `· ${d}` : "";
    }

    if (checkoutD) {
        checkoutD.textContent =
            d ? `· ${d}` : "";
    }

    const feeEl =
        document.getElementById(
            "deliveryFeeDisplay"
        );

    const checkoutFeeEl =
        document.getElementById(
            "checkoutDeliveryFee"
        );

    if (feeEl) {
        feeEl.textContent =
            formatDelivery(deliveryFee);
    }

    if (checkoutFeeEl) {
        checkoutFeeEl.textContent =
            formatDelivery(deliveryFee);
    }
}


let customerLocationRequest = null;


function requestCustomerLocation(forceRefresh=false) {

    return new Promise((resolve, reject) => {

        if (!config.deliveryEnabled) {
            resolve(null);
            return;
        }

        if (customerLocation && !forceRefresh) {
            resolve(customerLocation);
            return;
        }

        if (customerLocationRequest) {
            customerLocationRequest
                .then(resolve)
                .catch(reject);
            return;
        }

        if (!navigator.geolocation) {
            reject(
                new Error(
                    "Tu navegador no permite obtener la ubicación."
                )
            );
            return;
        }

        const status =
            document.getElementById(
                "customerLocationStatus"
            );

        if (status) {
            status.textContent =
                " Obteniendo ubicación…";
        }

        customerLocationRequest =
            new Promise(
                (resolveRequest, rejectRequest) => {

                    let finished = false;

                    const finish =
                        (callback, value) => {

                            if (finished) return;

                            finished = true;

                            callback(value);
                        };

                    const timeoutId =
                        setTimeout(() => {

                            finish(
                                rejectRequest,
                                new Error(
                                    "La ubicación tardó demasiado. Inténtalo nuevamente."
                                )
                            );

                        }, 12000);

                    navigator.geolocation.getCurrentPosition(

                        pos => {

                            clearTimeout(timeoutId);

                            customerLocation = {
                                latitude:
                                    pos.coords.latitude,

                                longitude:
                                    pos.coords.longitude
                            };

                            if (status) {
                                status.textContent =
                                    " ✓ Ubicación obtenida correctamente.";
                            }

                            finish(
                                resolveRequest,
                                customerLocation
                            );
                        },

                        err => {

                            clearTimeout(timeoutId);

                            const messages = {
                                1:
                                    "Permiso de ubicación denegado. Actívalo en el navegador.",

                                2:
                                    "No fue posible obtener tu ubicación.",

                                3:
                                    "La ubicación tardó demasiado. Inténtalo nuevamente."
                            };

                            const message =
                                messages[err.code] ||
                                "No fue posible obtener tu ubicación.";

                            if (status) {
                                status.textContent =
                                    " " + message;
                            }

                            finish(
                                rejectRequest,
                                new Error(message)
                            );
                        },

                        {
                            enableHighAccuracy: true,
                            timeout: 10000,
                            maximumAge: 60000
                        }
                    );
                }
            );

      customerLocationRequest.then(
        value => { customerLocationRequest=null; resolve(value); },
        error => { customerLocationRequest=null; reject(error); }
      );
    });
  }

  async function quoteDelivery() {
    if(!config.deliveryEnabled) {
      deliveryFee = 0;
      deliveryDistanceKm = 0;
      updateDeliveryUI();
      return true;
    }
    if(config.latitude == null || config.longitude == null) {
      throw new Error("Este negocio aún no tiene configurada su ubicación para calcular el envío.");
    }
    const location = await requestCustomerLocation();
    const r = await fetch(`/api/cotizar-envio/${config.id}?lat=${encodeURIComponent(location.latitude)}&lon=${encodeURIComponent(location.longitude)}`);
    const data = await r.json();
    if(!r.ok || !data.ok) throw new Error(data.error || "No se pudo calcular el envío.");
    deliveryFee = Number(data.delivery_fee || 0);
    deliveryDistanceKm = Number(data.distance_km || 0);
    updateDeliveryUI();
    return true;
  }

  function renderCart() {
    const qty = cart.reduce((s,x)=>s+x.qty,0);
    countEls.forEach(el=>el.textContent=qty);
    const sub=subtotal(), com=commission(), total=grandTotal();
const discountRow =
  document.getElementById("couponDiscountRow");

const discountDisplay =
  document.getElementById("couponDiscountDisplay");

if(discountDisplay) {
  discountDisplay.textContent =
    "-" + money(couponDiscount);
}

if(discountRow) {
  discountRow.hidden = !appliedCoupon;
}
    if(subtotalEl) subtotalEl.textContent=money(sub);
    const commissionEl=document.getElementById("commissionDisplay");
    if(commissionEl) commissionEl.textContent=money(com);
    const checkoutCommission=document.getElementById("checkoutCommission");
    if(checkoutCommission) checkoutCommission.textContent=money(com);
    if(totalEl) totalEl.textContent=money(total);
    if(checkoutBtn) {
  checkoutBtn.disabled =
    cart.length === 0 ||
    (minimumOrder > 0 && sub < minimumOrder);
}
    if(!itemsEl) return;
    if(!cart.length){itemsEl.innerHTML='<p class="empty">Aún no agregas productos.</p>';return;}
    itemsEl.innerHTML=cart.map(x=>`
      <div class="cart-item">
        <div><b>${x.name}</b><small>${money(x.price)} · ${x.qty} pieza(s)</small></div>
        <div class="qty">
  <button type="button" data-action="minus" data-id="${x.id}">−</button>
  <span>${x.qty}</span>
  <button type="button" data-action="plus" data-id="${x.id}">+</button>
  <button type="button" class="cart-remove" data-action="remove" data-id="${x.id}">
    🗑
  </button>
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

if(itemsEl) itemsEl.addEventListener("click", e => {
    const b = e.target.closest("button[data-id]");
    if (!b) return;

    const id = Number(b.dataset.id);
    const x = cart.find(i => i.id === id);
    if (!x) return;

    const action = b.dataset.action;

    if (action === "plus") {
        x.qty += 1;
    }

    if (action === "minus") {
        x.qty -= 1;

        if (x.qty <= 0) {
            cart.splice(cart.indexOf(x), 1);
        }
    }

    if (action === "remove") {
        cart.splice(cart.indexOf(x), 1);
    }

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

  const getCustomerLocationBtn=document.getElementById("getCustomerLocation");
  const customerLocationStatus=document.getElementById("customerLocationStatus");
  if(getCustomerLocationBtn){
    getCustomerLocationBtn.addEventListener("click",async()=>{
      if(getCustomerLocationBtn.dataset.loading==="1") return;

      getCustomerLocationBtn.dataset.loading="1";
      getCustomerLocationBtn.disabled=true;
      if(customerLocationStatus) customerLocationStatus.textContent=" Obteniendo ubicación…";

      try{
        await requestCustomerLocation();
        await quoteDelivery();

        const sub=subtotal(), com=commission();
        if(checkoutSubtotal) checkoutSubtotal.textContent=money(sub);
        const checkoutDeliveryFee=document.getElementById("checkoutDeliveryFee");
        if(checkoutDeliveryFee) checkoutDeliveryFee.textContent=money(deliveryFee);
        const checkoutCommission=document.getElementById("checkoutCommission");
        if(checkoutCommission) checkoutCommission.textContent=money(com);
        if(checkoutTotal) checkoutTotal.textContent=money(grandTotal());
      }catch(ex){
        if(customerLocationStatus) {
          customerLocationStatus.textContent=" "+(ex.message||"No fue posible obtener tu ubicación.");
        }
        if(err) err.textContent=ex.message || "No se pudo calcular el envío.";
      }finally{
        getCustomerLocationBtn.dataset.loading="0";
        getCustomerLocationBtn.disabled=false;
      }
    });
  }

  const modal=document.getElementById("checkoutModal"), close=document.getElementById("closeCheckout");
  const form=document.getElementById("checkoutForm"), err=document.getElementById("checkoutError"), checkoutTotal=document.getElementById("checkoutTotal");

  if(checkoutBtn && modal && form){
    checkoutBtn.addEventListener("click",async()=>{
      if(!cart.length)return;
      err.textContent="";
      checkoutBtn.disabled=true;
      try {
        // Abrimos el formulario sin solicitar GPS automáticamente.
        // El cliente decide cuándo obtener su ubicación con el botón.
        const sub=subtotal(), com=commission();
        if(checkoutSubtotal) checkoutSubtotal.textContent=money(sub);
        const checkoutDeliveryFee=document.getElementById("checkoutDeliveryFee");
        if(checkoutDeliveryFee) checkoutDeliveryFee.textContent=money(deliveryFee);
        const checkoutCommission=document.getElementById("checkoutCommission");
        if(checkoutCommission) checkoutCommission.textContent=money(com);
        checkoutTotal.textContent=money(grandTotal());
        modal.classList.remove("hidden");
      } catch(ex) {
        err.textContent=ex.message || "No se pudo preparar el pedido.";
        modal.classList.remove("hidden");
      } finally {
        checkoutBtn.disabled=cart.length===0;
      }
    });
    close.addEventListener("click",()=>modal.classList.add("hidden"));
    modal.addEventListener("click",e=>{if(e.target===modal)modal.classList.add("hidden")});

    form.addEventListener("submit",async e=>{
      e.preventDefault(); err.textContent="";
      const fd=new FormData(form);
      const payload={
  business_id:window.NEGOCIO.id,

  customer:{
    name:fd.get("name"),
    phone:fd.get("phone"),
    email:fd.get("email"),
    address:fd.get("address"),
    payment_method:fd.get("payment_method"),
    notes:fd.get("notes"),
    latitude:customerLocation?.latitude,
    longitude:customerLocation?.longitude
  },

  items:cart.map(x=>({
    id:x.id,
    qty:x.qty
  })),

  coupon_id: appliedCoupon ? appliedCoupon.id : null,
  coupon_code: appliedCoupon ? appliedCoupon.code : null
};

      try{
        await quoteDelivery();
        const csrfToken=document.querySelector('meta[name="csrf-token"]')?.getAttribute("content");
        const r=await fetch("/pedido/crear",{method:"POST",headers:{"Content-Type":"application/json","X-CSRFToken":csrfToken||""},body:JSON.stringify(payload)});
        const data=await r.json();
        if(!data.ok){err.textContent=data.error||"No se pudo registrar el pedido.";return;}
        if(fd.get("payment_method")==="Tarjeta (PayU)"){
          window.location.href="/pago/payu/"+data.order_id;
        }else if(fd.get("payment_method")==="Tarjeta (Conekta)"){
          window.location.href="/pago/conekta/"+data.order_id;
        }else{
          window.location.href="/pedido/"+data.order_id+"/confirmado";
        }
      }catch(ex){err.textContent=ex.message || "No se pudo completar el pedido.";}
    });
  }
  updateDeliveryUI();
  renderCart();
});
