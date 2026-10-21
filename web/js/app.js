'use strict';
// Telegram Mini App магазина. Каталог и цены приходят с бэкенда (GET api/catalog).

const tg = window.Telegram?.WebApp;
if (tg) {
    tg.expand();
    tg.ready();
    tg.setHeaderColor?.('#000000');
    tg.setBackgroundColor?.('#000000');
    tg.setBottomBarColor?.('#000000');
}

const MAX_QTY = 10; // как MAX_ITEM_QTY в app/config.py
const CART_KEY = 'tg_shop_cart';
const CHECKOUT_KEY = 'tg_shop_checkout';
const STATUS = {
    preorder: { order: 1, text: 'Предзаказ', css: 'preorder' },
    in_stock: { order: 2, text: 'В наличии', css: 'in-stock' },
    sold_out: { order: 3, text: 'Нет в наличии', css: 'out' },
};
// Коллекции широкой версии каталога. Модель одна на коллекцию, а её цвета — это отдельные
// товары из catalog.json (id), поэтому корзина, цены и заказы работают как раньше.
const COLLECTIONS = [
    {
        name: 'Angel’s Advice collection',
        text: 'Мое стремление, действие и творчество - подобно совету ангела, который наставляет меня на верный путь. Крик в неизвестность, с целью обрести эхо в лице тех, кто тоже хочет говорить',
        date: '06.10.25',
        photo: 'assets/img/collection-zip.webp',
        focus: '50% 100%', // какая часть фото остаётся в кадре — человек внизу снимка
        title: 'Zip-Hoodie Angel’s Advice',
        colors: [{ label: 'Black', id: 1 }, { label: 'Grey', id: 2 }],
    },
    {
        name: 'Without designer collection',
        text: '',
        date: '',
        photo: 'assets/img/collection-long.webp',
        focus: '50% 50%',
        title: 'Longsleeve Harnessed Chaos',
        colors: [{ label: 'Black', id: 4 }, { label: 'White', id: 3 }],
    },
];
const collectionOf = (id) => COLLECTIONS.find((c) => c.colors.some((x) => x.id === id));
const CHECKOUT_FIELDS = ['name', 'surname', 'patronymic', 'phone', 'telegram', 'email', 'comment', 'address'];
const EMPTY_ICON = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="M8 11V9a1 1 0 0 1 2 0v2a1 1 0 0 1-2 0zm7 1a1 1 0 0 0 1-1V9a1 1 0 0 0-2 0v2a1 1 0 0 0 1 1zm-3 2a6.036 6.036 0 0 0-4.775 2.368 1 1 0 1 0 1.55 1.264 4 4 0 0 1 6.45 0 1 1 0 0 0 1.55-1.264A6.036 6.036 0 0 0 12 14zm11-2A11 11 0 1 1 12 1a11.013 11.013 0 0 1 11 11zm-2 0a9 9 0 1 0-9 9 9.01 9.01 0 0 0 9-9z"></path></svg>`;

// --- утилиты ---

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const rub = (n) => `${Number(n).toLocaleString('ru-RU')} ₽`;
const haptic = (style = 'light') => tg?.HapticFeedback?.impactOccurred(style);
const hapticResult = (type = 'success') => tg?.HapticFeedback?.notificationOccurred(type);

const store = {
    get(key, fallback) {
        try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
    },
    set(key, value) {
        try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* приватный режим */ }
    },
    remove(key) {
        try { localStorage.removeItem(key); } catch { /* приватный режим */ }
    },
};

const emptyMessage = (text) => `<div class="empty-cart-msg"><span>${text}</span>${EMPTY_ICON}</div>`;

// --- состояние ---

let products = [];
let cart = store.get(CART_KEY, []);
let currentProduct = null;
let selectedSize = null;
let currentSlideIdx = 0;
let currentLightboxIdx = 0;
let historyStack = ['landing'];
// К какому разделу шапки относится экран: кнопку своего раздела шапка не показывает.
const SECTION = {
    landing: 'main', catalog: 'cloth', product: 'cloth', brand: 'about',
    cart: 'cart', checkout: 'cart', qr: 'cart', orders: 'cart',
};
let customAlertCallback = null;
let submitting = false;

function saveCart() {
    store.set(CART_KEY, cart);
}

// Сверяет корзину с каталогом: убирает недоступное, обновляет цены и картинки.
function syncCart() {
    if (!products.length) return;
    const before = JSON.stringify(cart);
    cart = (Array.isArray(cart) ? cart : []).flatMap((item) => {
        const p = products.find((x) => x.id === item.id);
        if (!p || p.status === 'sold_out' || !p.sizes.includes(item.size)) return [];
        const quantity = Math.min(Math.max(parseInt(item.quantity, 10) || 1, 1), MAX_QTY);
        return [{ id: p.id, title: p.title, price: p.price, image: p.images[0], size: item.size, quantity }];
    });
    if (JSON.stringify(cart) !== before) saveCart();
}

// --- навигация ---

// «Назад»: кнопка Telegram и крестик в карточке товара ведут на предыдущий экран.
function goBack() {
    const current = historyStack[historyStack.length - 1];
    if (current === 'lightbox') closeLightbox();
    else if (current === 'custom-alert') closeCustomAlert();
    else if (historyStack.length > 1) {
        historyStack.pop();
        renderScreenInternal(historyStack[historyStack.length - 1]);
        haptic('light');
    }
}

tg?.BackButton?.onClick(goBack);

function updateBackButton() {
    if (historyStack.length > 1) tg?.BackButton?.show();
    else tg?.BackButton?.hide();
}

function navigate(screenId) {
    if (historyStack[historyStack.length - 1] === screenId) return;
    const existingIdx = historyStack.indexOf(screenId);
    if (existingIdx !== -1) historyStack = historyStack.slice(0, existingIdx + 1);
    else historyStack.push(screenId);
    renderScreenInternal(screenId);
    haptic('light');
}

// Корень истории — стартовый экран; «назад» после заказа ведёт в каталог, а не к оформлению.
function resetTo(screenId) {
    historyStack = ['landing', 'catalog', screenId].slice(0, screenId === 'landing' ? 1 : screenId === 'catalog' ? 2 : 3);
    renderScreenInternal(screenId);
}

function renderScreenInternal(screenId) {
    syncCart();
    document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
    const target = $(`${screenId}-screen`);
    if (target) {
        target.classList.add('active');
        target.scrollTop = 0;
    }
    document.body.dataset.screen = screenId;
    document.body.dataset.section = SECTION[screenId] || '';
    syncBrandVideo();
    if (screenId === 'catalog') {
        renderCatalog();
        armCollectionHover();
    }
    if (screenId === 'cart') renderCart();
    if (screenId === 'product' && currentProduct) updateButtonInterface();
    if (screenId === 'orders') fetchAndRenderOrders();
    updateBackButton();
}

// --- видео в разделе About ---

// Видео показывается только на широком экране (как в CSS); на телефоне его не грузим вовсе.
const WIDE_MAIN = window.matchMedia('(min-width: 900px) and (min-aspect-ratio: 1/1)');
const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');

function syncBrandVideo() {
    const video = document.querySelector('.brand-video');
    if (!video) return;
    const onAbout = historyStack[historyStack.length - 1] === 'brand';
    if (onAbout && WIDE_MAIN.matches && !REDUCED_MOTION.matches) {
        if (!video.getAttribute('src')) video.src = video.dataset.src;
        video.play().catch(() => { /* автоплей запрещён — остаётся постер */ });
    } else {
        video.pause();
    }
}

WIDE_MAIN.addEventListener?.('change', syncBrandVideo);

// --- модальное уведомление ---

function showCustomAlert(message, callback = null) {
    $('custom-alert-text').innerText = message;
    customAlertCallback = callback;
    $('custom-alert-modal').classList.add('active');
    historyStack.push('custom-alert');
    tg?.BackButton?.show();
    haptic('medium');
    $('custom-alert-btn').onclick = closeCustomAlert;
}

function closeCustomAlert() {
    $('custom-alert-modal').classList.remove('active');
    if (historyStack[historyStack.length - 1] === 'custom-alert') historyStack.pop();
    updateBackButton();
    if (customAlertCallback) {
        const cb = customAlertCallback;
        customAlertCallback = null;
        cb();
    }
}

// --- каталог и товар ---

async function loadCatalog() {
    const grid = $('products-grid');
    grid.innerHTML = '<div class="loading-msg">Загрузка...</div>';
    try {
        const response = await fetch('api/catalog');
        if (!response.ok) throw new Error(response.status);
        const data = await response.json();
        products = data.products.sort((a, b) => STATUS[a.status].order - STATUS[b.status].order);
        renderScreenInternal(historyStack[historyStack.length - 1]);
    } catch {
        grid.innerHTML = `<div class="loading-msg">Не удалось загрузить каталог.
            <button class="main-action-btn" onclick="loadCatalog()">Повторить</button></div>`;
    }
}

function priceHTML(p, prefix) {
    const old = p.old_price && p.old_price > p.price;
    return `<span class="${prefix}price">${rub(p.price)}</span>${old ? `<span class="${prefix}old-price">${rub(p.old_price)}</span>` : ''}`;
}

function renderCatalog() {
    const grid = $('products-grid');
    if (!products.length) return;
    grid.innerHTML = '';
    products.forEach((p) => {
        const card = document.createElement('div');
        card.className = 'product-card';
        card.onclick = () => openProductPage(p.id);
        const old = p.old_price && p.old_price > p.price;
        card.innerHTML = `
            <div class="product-card-img-wrapper"><img src="${esc(p.images[0])}" alt="${esc(p.title)}"></div>
            <div class="product-card-info">
                <h3>${esc(p.title)}</h3>
                <div class="product-card-price-container ${old ? 'space-between' : 'center'}">${priceHTML(p, '')}</div>
                <div class="product-status status-${STATUS[p.status].css}">${STATUS[p.status].text}</div>
            </div>`;
        grid.appendChild(card);
    });
    renderCollections();
    updateCartBadge();
}

// Наведение на фото коллекции включается только после настоящего движения мыши:
// иначе курсор, оказавшийся над фото при открытии каталога, сразу показал бы студийное фото.
function armCollectionHover() {
    const box = $('collections');
    box.classList.remove('hover-ready');
    let origin = null;
    const onMove = (e) => {
        if (e.pointerType !== 'mouse') return;
        if (!origin) { origin = { x: e.clientX, y: e.clientY }; return; }
        if (Math.hypot(e.clientX - origin.x, e.clientY - origin.y) < 3) return; // синтетический mousemove без сдвига
        box.classList.add('hover-ready');
        document.removeEventListener('pointermove', onMove);
    };
    document.removeEventListener('pointermove', armCollectionHover.onMove || onMove);
    armCollectionHover.onMove = onMove;
    document.addEventListener('pointermove', onMove);
}

// Блок коллекции: слева название/описание/дата, справа фото (через раз — наоборот).
// При наведении фото сменяется студийным фото чёрного товара, клик по фото открывает карточку.
function renderCollections() {
    $('collections').innerHTML = COLLECTIONS.map((c, i) => {
        const black = products.find((p) => p.id === c.colors[0].id);
        if (!black) return '';
        return `
            <section class="collection${i % 2 ? ' reverse' : ''}">
                <div class="collection-text">
                    <h2 class="collection-name">${esc(c.name)}</h2>
                    ${c.text ? `<p class="collection-desc">${esc(c.text)}</p>` : '<div></div>'}
                    ${c.date ? `<div class="collection-date">${esc(c.date)}</div>` : '<div></div>'}
                </div>
                <button type="button" class="collection-photo" onclick="openProductPage(${black.id})" aria-label="${esc(c.title)}">
                    <img class="collection-shot" src="${esc(c.photo)}" alt="" style="object-position: ${esc(c.focus || '50% 50%')}">
                    <img class="collection-studio" src="${esc(black.images[0])}" alt="">
                </button>
            </section>`;
    }).join('');
}

// size — когда товар открывают из корзины: сразу выбран размер, который лежит в корзине.
function openProductPage(id, size = null) {
    if (!fillProduct(id, size)) return;
    // карточка в истории одна: открыли из корзины — крестик вернёт в корзину, а не в старую карточку
    historyStack = historyStack.filter((s) => s !== 'product');
    navigate('product');
}

// Заполняет карточку товаром id. Смена цвета на ПК вызывает её же — без новой записи в истории.
function fillProduct(id, size = null) {
    currentProduct = products.find((p) => p.id === id);
    if (!currentProduct) return false;
    const p = currentProduct;
    const coll = collectionOf(p.id);
    selectedSize = p.status !== 'sold_out' && p.sizes.includes(size) ? size : null;
    currentSlideIdx = 0;

    // на телефоне — полное название товара, на ПК — название модели (цвет выбирается кнопками)
    $('p-title').innerHTML = `<span class="title-narrow">${esc(p.title)}</span><span class="title-wide">${esc(coll ? coll.title : p.title)}</span>`;

    const colors = $('color-selector');
    colors.innerHTML = '';
    $('color-section').hidden = !coll;
    coll?.colors.forEach((c) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `size-chip color-chip${c.id === p.id ? ' selected' : ''}`;
        btn.innerText = c.label;
        btn.onclick = () => {
            if (c.id === currentProduct.id) return;
            fillProduct(c.id, selectedSize);
            haptic('light');
        };
        colors.appendChild(btn);
    });
    $('p-desc').innerHTML = ['Состав:', 'Рекомендации по уходу:', 'Стирка:', 'Глажка:']
        .reduce((html, h) => html.replaceAll(h, `<span class="desc-header">${h}</span>`), esc(p.desc))
        .replace(/\n/g, '<br>');
    $('p-status-container').innerHTML = `<div class="product-screen-status-badge badge-${STATUS[p.status].css}">${STATUS[p.status].text}</div>`;
    $('p-price-container').innerHTML = priceHTML(p, 'product-screen-');

    const track = $('carousel-track');
    const dots = $('carousel-dots');
    dots.innerHTML = '';
    fillLoopTrack(track, p.images, (src, idx) => {
        const img = document.createElement('img');
        img.src = src;
        img.alt = `${p.title}, фото ${idx + 1}`;
        img.onclick = () => openLightbox(idx);
        return img;
    });
    p.images.forEach((src, idx) => {
        const dot = document.createElement('div');
        dot.className = `dot ${idx === 0 ? 'active' : ''}`;
        dot.onclick = (e) => { e.stopPropagation(); setSlide(idx); };
        dots.appendChild(dot);
    });
    updateCarouselPosition(false);

    const sizes = $('size-selector');
    sizes.innerHTML = '';
    p.sizes.forEach((size) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = p.status === 'sold_out' ? 'size-chip disabled' : 'size-chip';
        btn.innerText = size;
        if (size === selectedSize) btn.classList.add('selected');
        if (p.status !== 'sold_out') {
            btn.onclick = () => {
                sizes.querySelectorAll('.size-chip').forEach((c) => c.classList.remove('selected'));
                btn.classList.add('selected');
                selectedSize = size;
                updateButtonInterface();
            };
        }
        sizes.appendChild(btn);
    });

    updateButtonInterface();
    return true;
}

// --- карусель и лайтбокс ---

function wrapIndex(idx) {
    const n = currentProduct.images.length;
    return (idx + n) % n;
}

// --- лента «по кругу» ---
// По краям ленты лежат копии крайних фото: 6 → 1 доезжает до копии первого и незаметно
// перескакивает на настоящее, вместо того чтобы прокручивать всю ленту назад.

// Сколько пикселей нужно протянуть, чтобы перелистнуть фото: в мини-аппе и на сайте по-разному.
const FLIP = tg?.initData ? { normal: 60, zoomed: 150 } : { normal: 90, zoomed: 180 };
// на очень узком экране порог не больше 40% ширины, иначе до него не дотянуться
const flipThreshold = (element, px) => Math.min(px, element.clientWidth * 0.4);

const loopOffset = () => (currentProduct && currentProduct.images.length > 1 ? 1 : 0);
const pendingJumps = new Map(); // track -> функция, завершающая незаконченный переход через край

function fillLoopTrack(track, images, makeImg) {
    pendingJumps.get(track)?.();
    track.innerHTML = '';
    const n = images.length;
    const order = n > 1 ? [n - 1, ...images.keys(), 0] : [0];
    order.forEach((i) => track.appendChild(makeImg(images[i], i)));
}

function slideTo(track, pos, animate = true) {
    track.style.transition = animate ? '' : 'none';
    track.style.transform = `translateX(-${(pos + loopOffset()) * 100}%)`;
}

// Шаг на одно фото; возвращает новый индекс. Через край — до копии, потом прыжок без анимации.
function stepTrack(track, from, dir) {
    pendingJumps.get(track)?.();
    const n = currentProduct.images.length;
    if (n < 2) return from;
    const raw = from + dir;
    const to = wrapIndex(raw);
    slideTo(track, raw, true);
    if (raw !== to) {
        const jump = (e) => {
            if (e && e.target !== track) return; // transitionend от зума картинки внутри ленты
            clearTimeout(timer);
            track.removeEventListener('transitionend', jump);
            pendingJumps.delete(track);
            slideTo(track, to, false);
            void track.offsetWidth; // применить позицию до возврата анимации
            track.style.transition = '';
        };
        const timer = setTimeout(jump, 400);
        track.addEventListener('transitionend', jump);
        pendingJumps.set(track, () => jump());
    }
    return to;
}

function moveCarousel(dir, event) {
    event?.stopPropagation();
    event?.preventDefault();
    if (!currentProduct) return;
    currentSlideIdx = stepTrack($('carousel-track'), currentSlideIdx, dir);
    updateDots();
}

function setSlide(idx) {
    currentSlideIdx = idx;
    updateCarouselPosition();
}

function updateCarouselPosition(animate = true) {
    const track = $('carousel-track');
    pendingJumps.get(track)?.();
    slideTo(track, currentSlideIdx, animate);
    updateDots();
}

function updateDots() {
    document.querySelectorAll('.carousel-dots .dot').forEach((dot, idx) => dot.classList.toggle('active', idx === currentSlideIdx));
}

function openLightbox(index) {
    if (!currentProduct) return;
    currentLightboxIdx = index;
    fillLoopTrack($('lightbox-track'), currentProduct.images, (src) => {
        const img = document.createElement('img');
        img.src = src;
        img.alt = '';
        return img;
    });
    updateLightboxPosition(false);
    $('lightbox-modal').classList.add('active');
    resetZoom();
    tg?.disableVerticalSwipes?.(); // иначе свайп вниз свернёт мини-апп вместо закрытия фото
    historyStack.push('lightbox');
    tg?.BackButton?.show();
    haptic('medium');
}

function closeLightbox() {
    resetZoom();
    const modal = $('lightbox-modal');
    modal.classList.remove('active');
    modal.style.transition = '';
    modal.style.backgroundColor = '';
    [...$('lightbox-track').children].forEach((img) => { img.style.opacity = ''; });
    tg?.enableVerticalSwipes?.();
    if (historyStack[historyStack.length - 1] === 'lightbox') historyStack.pop();
    updateBackButton();
    haptic('light');
}

function moveLightbox(dir, event) {
    event?.stopPropagation();
    event?.preventDefault();
    if (!currentProduct) return;
    resetZoom();
    currentLightboxIdx = stepTrack($('lightbox-track'), currentLightboxIdx, dir);
    updateLightboxCounter();
    haptic('light');
}

function updateLightboxPosition(animate = true) {
    const track = $('lightbox-track');
    pendingJumps.get(track)?.();
    slideTo(track, currentLightboxIdx, animate);
    updateLightboxCounter();
}

function updateLightboxCounter() {
    $('lightbox-counter').innerText = `${currentLightboxIdx + 1} / ${currentProduct.images.length}`;
}

// Листание карусели в карточке и пальцем, и мышью: лента едет за указателем,
// отпустил — следующее фото или возврат.
function bindSwipe(element) {
    const track = $('carousel-track');
    let pointerId = null;
    let startX = 0;
    let startY = 0;
    let dx = 0;
    let dragging = false;
    let suppressClick = false;

    element.addEventListener('pointerdown', (e) => {
        if (e.button !== 0 || e.target.closest('button') || !currentProduct) return;
        if (e.pointerType === 'mouse') e.preventDefault(); // без «призрака» перетаскиваемой картинки
        pendingJumps.get(track)?.();
        pointerId = e.pointerId;
        startX = e.clientX;
        startY = e.clientY;
        dx = 0;
        dragging = false;
    });

    element.addEventListener('pointermove', (e) => {
        if (e.pointerId !== pointerId) return;
        dx = e.clientX - startX;
        if (!dragging && Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(e.clientY - startY)) {
            dragging = true;
            element.setPointerCapture?.(pointerId);
            element.classList.add('dragging');
            track.style.transition = 'none';
        }
        if (dragging) track.style.transform = `translateX(calc(${-(currentSlideIdx + loopOffset()) * 100}% + ${dx}px))`;
    });

    const finish = (e) => {
        if (e.pointerId !== pointerId) return;
        pointerId = null;
        if (!dragging) return;
        dragging = false;
        element.classList.remove('dragging');
        track.style.transition = '';
        // после перетаскивания браузер шлёт click — он не должен открыть лайтбокс
        suppressClick = true;
        setTimeout(() => { suppressClick = false; }, 0);
        if (e.type === 'pointerup' && Math.abs(dx) > flipThreshold(element, FLIP.normal)) {
            moveCarousel(dx < 0 ? 1 : -1);
            haptic('light');
        } else {
            updateCarouselPosition();
        }
    };
    element.addEventListener('pointerup', finish);
    element.addEventListener('pointercancel', finish);
    element.addEventListener('click', (e) => {
        if (suppressClick) { e.stopPropagation(); e.preventDefault(); }
    }, true);
}

// --- полноэкранный просмотр: листание, зум и перемещение ---
// Телефон: щипок — зум, двойной тап — приблизить/вернуть, пальцем — двигать увеличенное фото.
// ПК: клик — приблизить в точку/вернуть, зажал и тянешь — двигать, колесо — зум.
// Без зума горизонтальный жест листает фото; тап мимо картинки закрывает просмотр.

const ZOOM_MAX = 4;
const ZOOM_TAP = 2.5;
const ZOOM_DISMISS = 150; // с зумом: на сколько протянуть вниз за верхний край фото, чтобы закрыть просмотр
let zoom = { s: 1, x: 0, y: 0 };

function lightboxImg() {
    return $('lightbox-track').children[currentLightboxIdx + loopOffset()];
}

function lightboxCenter() {
    const r = $('lightbox-carousel').getBoundingClientRect();
    return { cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
}

// Размер самой картинки внутри object-fit: contain (без пустых полей по краям).
function imageBox(img) {
    const W = img.clientWidth;
    const H = img.clientHeight;
    const ratio = (img.naturalWidth || 1) / (img.naturalHeight || 1);
    const w = Math.min(W, H * ratio);
    return { W, H, w, h: w / ratio };
}

function applyZoom(animate) {
    const img = lightboxImg();
    if (!img) return;
    const { W, H, w, h } = imageBox(img);
    // по горизонтали край увеличенного фото можно отвести от края экрана на четверть экрана,
    // дальше — перелистывание; по вертикали запаса нет: дальше края — уже закрытие свайпом вниз
    const maxX = zoom.s > 1 ? Math.max(0, (zoom.s * w - W) / 2 + W / 4) : 0;
    const maxY = Math.max(0, (zoom.s * h - H) / 2);
    zoom.x = Math.min(maxX, Math.max(-maxX, zoom.x));
    zoom.y = Math.min(maxY, Math.max(-maxY, zoom.y));
    img.style.transition = animate ? 'transform 0.25s ease' : 'none';
    img.style.transform = zoom.s > 1 ? `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.s})` : '';
    $('lightbox-modal').classList.toggle('zoomed', zoom.s > 1);
}

function resetZoom(animate = false) {
    zoom = { s: 1, x: 0, y: 0 };
    [...$('lightbox-track').children].forEach((img) => { img.style.transform = ''; });
    applyZoom(animate);
}

// Новый масштаб s так, чтобы точка экрана (px, py) осталась под пальцем/курсором.
function zoomAt(px, py, s, animate) {
    const { cx, cy } = lightboxCenter();
    s = Math.min(ZOOM_MAX, Math.max(1, s));
    const qx = (px - cx - zoom.x) / zoom.s;
    const qy = (py - cy - zoom.y) / zoom.s;
    zoom = s === 1 ? { s: 1, x: 0, y: 0 } : { s, x: px - cx - s * qx, y: py - cy - s * qy };
    applyZoom(animate);
}

function toggleZoom(px, py) {
    if (zoom.s > 1) resetZoom(true);
    else zoomAt(px, py, ZOOM_TAP, true);
    haptic('light');
}

function pointOnImage(px, py) {
    const img = lightboxImg();
    if (!img) return false;
    const { cx, cy } = lightboxCenter();
    const { w, h } = imageBox(img);
    return Math.abs(px - cx) <= w / 2 && Math.abs(py - cy) <= h / 2;
}

// --- закрытие свайпом вниз: фото едет за пальцем, фон светлеет ---

// Работает и с зумом: фото уезжает вниз от текущего положения и масштаба.
function dragDismiss(dy) {
    const img = lightboxImg();
    if (!img) return;
    const d = Math.max(0, dy);
    const k = Math.min(d / window.innerHeight, 1);
    img.style.transition = 'none';
    img.style.transform = `translate(${zoom.x}px, ${zoom.y + d}px) scale(${zoom.s * (1 - k * 0.15)})`;
    $('lightbox-modal').style.backgroundColor = `rgba(0, 0, 0, ${1 - Math.min(k * 1.6, 1) * 0.75})`;
}

function restoreDismiss() {
    $('lightbox-modal').style.backgroundColor = '';
    if (zoom.s > 1) {
        applyZoom(true);
        return;
    }
    const img = lightboxImg();
    if (img) {
        img.style.transition = 'transform 0.25s ease';
        img.style.transform = '';
    }
}

function dismissLightbox() {
    const img = lightboxImg();
    const modal = $('lightbox-modal');
    if (img) {
        img.style.transition = 'transform 0.2s ease-in, opacity 0.2s ease-in';
        img.style.transform = `translate(${zoom.x}px, ${zoom.y + window.innerHeight * 0.6}px) scale(${zoom.s * 0.8})`;
        img.style.opacity = '0';
    }
    modal.style.transition = 'background-color 0.2s ease-in';
    modal.style.backgroundColor = 'rgba(0, 0, 0, 0)';
    setTimeout(closeLightbox, 190);
}

function bindLightboxGestures(element) {
    const track = $('lightbox-track');
    const pointers = new Map();
    let mode = null; // 'tap' | 'swipe' | 'dismiss' | 'pan' | 'pinch'
    let start = null; // { x, y, zx, zy } — где начался жест и где было фото
    let moved = false; // палец/мышь сдвинулись — это уже не тап
    let overshoot = 0; // насколько увеличенное фото тянут дальше его края (для перелистывания)
    let overshootDown = 0; // насколько увеличенное фото тянут вниз дальше верхнего края (для закрытия)
    let pinch = null; // { d, s, qx, qy }
    let lastTap = { t: 0, x: 0, y: 0 };

    const distance = (a, b) => Math.hypot(a.x - b.x, a.y - b.y);
    const midpoint = (a, b) => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });

    function beginOnePointer(p) {
        start = { x: p.x, y: p.y, zx: zoom.x, zy: zoom.y };
        mode = zoom.s > 1 ? 'pan' : 'tap';
        moved = false;
        overshoot = 0;
        overshootDown = 0;
    }

    function beginPinch() {
        const [a, b] = [...pointers.values()];
        const m = midpoint(a, b);
        const { cx, cy } = lightboxCenter();
        if (mode === 'swipe') updateLightboxPosition();
        pinch = { d: distance(a, b) || 1, s: zoom.s, qx: (m.x - cx - zoom.x) / zoom.s, qy: (m.y - cy - zoom.y) / zoom.s };
        mode = 'pinch';
    }

    element.addEventListener('pointerdown', (e) => {
        if (e.button !== 0 || !currentProduct) return;
        if (e.pointerType === 'mouse') e.preventDefault();
        pendingJumps.get(track)?.();
        element.setPointerCapture?.(e.pointerId);
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (pointers.size === 1) beginOnePointer(pointers.get(e.pointerId));
        else if (pointers.size === 2) beginPinch();
        element.classList.toggle('panning', mode === 'pan');
    });

    element.addEventListener('pointermove', (e) => {
        if (!pointers.has(e.pointerId)) return;
        pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (mode === 'pinch' && pointers.size >= 2) {
            const [a, b] = [...pointers.values()];
            const m = midpoint(a, b);
            const { cx, cy } = lightboxCenter();
            const s = Math.min(ZOOM_MAX, Math.max(1, pinch.s * distance(a, b) / pinch.d));
            zoom = { s, x: m.x - cx - s * pinch.qx, y: m.y - cy - s * pinch.qy };
            applyZoom(false);
            return;
        }
        const dx = e.clientX - start.x;
        const dy = e.clientY - start.y;
        if (Math.hypot(dx, dy) > 8) moved = true;
        if (mode === 'pan') {
            zoom.x = start.zx + dx;
            zoom.y = start.zy + dy;
            applyZoom(false);
            // фото упёрлось в край — остаток движения тянет ленту, из-за края выезжает соседнее фото
            overshoot = currentProduct.images.length > 1 ? start.zx + dx - zoom.x : 0;
            overshootDown = Math.max(0, start.zy + dy - zoom.y);
            track.style.transition = 'none';
            track.style.transform = `translateX(calc(${-(currentLightboxIdx + loopOffset()) * 100}% + ${overshoot}px))`;
            if (overshootDown) dragDismiss(overshootDown);
            else $('lightbox-modal').style.backgroundColor = '';
        } else if (mode === 'tap' && Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) {
            mode = 'swipe';
            track.style.transition = 'none';
        } else if (mode === 'tap' && dy > 8 && dy > Math.abs(dx)) {
            mode = 'dismiss';
        }
        if (mode === 'swipe') track.style.transform = `translateX(calc(${-(currentLightboxIdx + loopOffset()) * 100}% + ${dx}px))`;
        if (mode === 'dismiss') dragDismiss(dy);
    });

    const finish = (e) => {
        if (!pointers.has(e.pointerId)) return;
        const p = pointers.get(e.pointerId);
        pointers.delete(e.pointerId);
        const cancelled = e.type === 'pointercancel';

        if (mode === 'pinch') {
            if (pointers.size === 1) {
                beginOnePointer([...pointers.values()][0]); // дальше двигаем оставшимся пальцем
                moved = true; // конец щипка — не тап, иначе отпускание пальца закрыло бы просмотр
            }
            else if (!pointers.size) mode = null;
            if (zoom.s < 1.05) resetZoom(true);
            return;
        }
        if (pointers.size) return;
        element.classList.remove('panning');

        if (mode === 'swipe') {
            const dx = p.x - start.x;
            track.style.transition = '';
            if (!cancelled && Math.abs(dx) > flipThreshold(element, FLIP.normal)) moveLightbox(dx < 0 ? 1 : -1);
            else updateLightboxPosition();
        } else if (mode === 'dismiss') {
            const dy = p.y - start.y;
            if (!cancelled && dy > Math.min(120, element.clientHeight * 0.2)) dismissLightbox();
            else restoreDismiss();
        } else if (mode === 'pan' && overshootDown > 0 && !cancelled && overshootDown > ZOOM_DISMISS) {
            dismissLightbox();
        } else if (mode === 'pan' && (overshoot || overshootDown)) {
            if (overshootDown) restoreDismiss();
            // с зумом перелистнуть можно, но тянуть нужно дальше, чем без него
            track.style.transition = '';
            if (!cancelled && Math.abs(overshoot) > flipThreshold(element, FLIP.zoomed)) moveLightbox(overshoot < 0 ? 1 : -1);
            else slideTo(track, currentLightboxIdx, true);
        } else if ((mode === 'tap' || mode === 'pan') && !moved && !cancelled) {
            handleTap(p, e.pointerType);
        }
        mode = null;
    };

    function handleTap(p, type) {
        if (zoom.s === 1 && !pointOnImage(p.x, p.y)) {
            closeLightbox();
            return;
        }
        if (type === 'mouse') {
            toggleZoom(p.x, p.y);
            return;
        }
        const now = Date.now();
        if (now - lastTap.t < 300 && Math.hypot(p.x - lastTap.x, p.y - lastTap.y) < 40) {
            toggleZoom(p.x, p.y);
            lastTap = { t: 0, x: 0, y: 0 };
        } else {
            lastTap = { t: now, x: p.x, y: p.y };
        }
    }

    element.addEventListener('pointerup', finish);
    element.addEventListener('pointercancel', finish);
    element.addEventListener('wheel', (e) => {
        if (!currentProduct) return;
        e.preventDefault();
        zoomAt(e.clientX, e.clientY, zoom.s * Math.exp(-e.deltaY * 0.0015), false);
    }, { passive: false });
}

// Стрелки клавиатуры листают фото, Esc закрывает полноэкранный просмотр.
document.addEventListener('keydown', (e) => {
    if (!currentProduct || e.target.closest('input, textarea')) return;
    const top = historyStack[historyStack.length - 1];
    const dir = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0;
    if (top === 'lightbox') {
        if (dir) moveLightbox(dir);
        else if (e.key === 'Escape') closeLightbox();
    } else if (top === 'product' && dir) {
        moveCarousel(dir);
    }
});

// --- корзина ---

function cartItem() {
    return cart.find((i) => i.id === currentProduct.id && i.size === selectedSize);
}

function updateButtonInterface() {
    const wrapper = $('product-action-wrapper');
    if (!wrapper || !currentProduct) return;
    if (currentProduct.status === 'sold_out') {
        wrapper.innerHTML = '<div class="out-of-stock-notice">Товар раскуплен</div>';
        return;
    }
    const item = selectedSize && cartItem();
    wrapper.innerHTML = item
        ? `<div class="split-action-block">
               <div class="split-quantity-picker">
                   <button onclick="changeCurrentProductQty(-1)">-</button>
                   <span>${item.quantity}</span>
                   <button onclick="changeCurrentProductQty(1)">+</button>
               </div>
               <button class="go-to-cart-btn" onclick="navigate('cart')">В корзину</button>
           </div>`
        : '<button class="main-action-btn" onclick="handleAddToCart()">Добавить в корзину</button>';
}

function handleAddToCart() {
    if (!currentProduct || currentProduct.status === 'sold_out') return;
    if (!selectedSize) {
        showCustomAlert('Пожалуйста, выберите размер!');
        return;
    }
    if (!cartItem()) {
        const p = currentProduct;
        cart.push({ id: p.id, title: p.title, price: p.price, image: p.images[0], size: selectedSize, quantity: 1 });
    }
    saveCart();
    updateCartBadge();
    updateButtonInterface();
    hapticResult('success');
}

// Меняет количество позиции; при 0 удаляет её. false — упёрлись в лимит.
function changeItemQty(item, delta) {
    if (item.quantity + delta > MAX_QTY) {
        showCustomAlert(`Не больше ${MAX_QTY} шт. одной позиции.`);
        return false;
    }
    item.quantity += delta;
    if (item.quantity <= 0) cart.splice(cart.indexOf(item), 1);
    saveCart();
    updateCartBadge();
    return true;
}

function changeCurrentProductQty(delta) {
    const item = currentProduct && selectedSize && cartItem();
    if (item && changeItemQty(item, delta)) {
        updateButtonInterface();
        haptic('light');
    }
}

function changeQty(idx, delta) {
    if (cart[idx] && changeItemQty(cart[idx], delta)) renderCart();
}

function deleteItem(idx) {
    cart.splice(idx, 1);
    saveCart();
    renderCart();
    updateCartBadge();
}

function openCartItem(idx) {
    if (cart[idx]) openProductPage(cart[idx].id, cart[idx].size);
}

function updateCartBadge() {
    const count = cart.reduce((sum, i) => sum + i.quantity, 0);
    document.querySelectorAll('.cart-count').forEach((el) => { el.innerText = count ? ` (${count})` : ''; });
}

function renderCart() {
    const container = $('cart-items-container');
    const checkoutBtn = $('checkout-main-btn');
    const total = cart.reduce((sum, i) => sum + i.price * i.quantity, 0);
    checkoutBtn.disabled = cart.length === 0;
    checkoutBtn.classList.toggle('disabled', cart.length === 0);
    $('cart-total-price').innerText = rub(total);
    $('cart-screen').classList.toggle('is-empty', !cart.length);

    if (!cart.length) {
        // пустая корзина: вместо списка — кнопка в каталог
        container.innerHTML = '<div class="cart-empty"><button type="button" class="cart-add" onclick="navigate(\'catalog\')">Add</button></div>';
        return;
    }
    container.innerHTML = cart.map((item, idx) => `
        <div class="cart-item">
            <img class="cart-item-link" src="${esc(item.image)}" alt="${esc(item.title)}" onclick="openCartItem(${idx})">
            <div class="cart-item-details">
                <h4><button type="button" class="cart-item-link" onclick="openCartItem(${idx})">${esc(item.title)}</button></h4>
                <p class="cart-item-meta">Размер: <b>${esc(item.size)}</b></p>
                <p class="cart-item-price">${rub(item.price * item.quantity)}</p>
            </div>
            <div class="cart-item-controls">
                <div class="quantity-picker">
                    <button onclick="changeQty(${idx}, -1)">-</button>
                    <span>${item.quantity}</span>
                    <button onclick="changeQty(${idx}, 1)">+</button>
                </div>
                <button class="delete-item-btn" onclick="deleteItem(${idx})">Удалить</button>
            </div>
        </div>`).join('');
}

// --- оформление заказа ---

function readCheckout() {
    const data = Object.fromEntries(CHECKOUT_FIELDS.map((f) => [f, $(`checkout-${f}`).value.trim()]));
    data.delivery = document.querySelector('input[name="delivery-method"]:checked')?.value || 'cdek';
    data.payment = document.querySelector('input[name="payment-method"]:checked')?.value || 'qr';
    return data;
}

function loadCheckoutData() {
    const data = store.get(CHECKOUT_KEY, null);
    if (!data) return;
    CHECKOUT_FIELDS.forEach((f) => { if (data[f]) $(`checkout-${f}`).value = data[f]; });
    for (const [name, value] of [['delivery-method', data.delivery], ['payment-method', data.payment]]) {
        const radio = document.querySelector(`input[name="${name}"][value="${CSS.escape(value || '')}"]`);
        if (radio) radio.checked = true;
    }
}

function clearCheckout() {
    store.remove(CHECKOUT_KEY);
    CHECKOUT_FIELDS.forEach((f) => { $(`checkout-${f}`).value = ''; });
}

async function processOrder() {
    if (submitting) return;
    syncCart();
    if (!cart.length) {
        resetTo('catalog');
        showCustomAlert('Ваша корзина пуста или товары были обновлены.');
        return;
    }
    const form = readCheckout();
    const required = ['name', 'surname', 'patronymic', 'telegram', 'email', 'address'];
    if (required.some((f) => !form[f]) || form.phone.length !== 15) {
        showCustomAlert('Пожалуйста, заполните все обязательные поля!');
        return;
    }

    const button = $('submit-order-btn');
    submitting = true;
    button.disabled = true;
    button.innerText = 'Отправка...';
    try {
        const response = await fetch('api/order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-TG-Init-Data': tg?.initData || '' },
            body: JSON.stringify({ ...form, cart: cart.map(({ id, size, quantity }) => ({ id, size, quantity })) }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.description || `Системный сбой сервера. Код: ${response.status}`);

        hapticResult('success');
        cart = [];
        saveCart();
        updateCartBadge();
        clearCheckout();
        if (data.order.qr) {
            $('payment-qr-img').src = data.order.qr;
            $('qr-total-amount').innerText = rub(data.order.total);
            resetTo('qr'); // «назад» с экрана оплаты ведёт в каталог, а не к повторной отправке
        } else {
            resetTo('catalog');
            showCustomAlert('Менеджер свяжется с вами!');
        }
    } catch (error) {
        const message = error instanceof TypeError ? 'нет связи с сервером' : error.message;
        showCustomAlert(`Ошибка при отправке заказа: ${message}`);
    } finally {
        submitting = false;
        button.disabled = false;
        button.innerText = 'Подтвердить заказ';
    }
}

function confirmQRPayment() {
    hapticResult('success');
    resetTo('catalog');
    showCustomAlert('Спасибо за покупку! Статус заказа обновится в ближайшее время. Отследить его можно в разделе Cart → Order.');
}

// --- мои заказы ---

function orderHTML(o) {
    const items = o.items
        .map((i, n) => `${n + 1}. ${esc(i.title)} (Разм: ${esc(i.size)}) — ${i.qty} шт. х ${i.price} руб. (${i.qty * i.price} руб.)`)
        .join('\n');
    const waiting = o.status === 'Ожидает подтверждения';
    return `
        <div class="cart-item order-card">
            <div class="order-head">
                <h4>Заказ #${o.id}</h4>
                <span class="order-status${waiting ? '' : ' order-status-active'}">${esc(o.status)}</span>
            </div>
            ${o.track ? `<div class="order-track">Трек-номер: ${esc(o.track)}</div>` : ''}
            <div class="order-details"><b>Телефон:</b> +7 ${esc(o.phone)}
<b>Telegram:</b> ${esc(o.telegram)}
<b>Email:</b> ${esc(o.email)}
<b>Способ доставки:</b> ${esc(o.delivery)}
<b>Адрес ПВЗ:</b> ${esc(o.address)}
<b>Способ оплаты:</b> ${esc(o.payment)}

<b>Состав заказа:</b>
${items}

<b>ИТОГО: ${rub(o.total)}.</b></div>
        </div>`;
}

async function fetchAndRenderOrders() {
    const container = $('orders-container');
    container.innerHTML = '<div class="loading-msg">Загрузка данных...</div>';
    try {
        const response = await fetch('api/orders', { headers: { 'X-TG-Init-Data': tg?.initData || '' } });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.description || `HTTP ${response.status}`);
        container.innerHTML = data.orders.length ? data.orders.map(orderHTML).join('') : emptyMessage('У вас пока нет заказов');
    } catch (error) {
        const message = error instanceof TypeError ? 'Сетевая ошибка при загрузке.' : error.message;
        container.innerHTML = `<div class="loading-msg error-msg">${esc(message)}</div>`;
    }
}

// --- инициализация ---

function formatPhone(value) {
    let digits = value.replace(/\D/g, '');
    if ((digits.startsWith('7') || digits.startsWith('8')) && digits.length > 10) digits = digits.substring(1);
    digits = digits.substring(0, 10);
    let out = '';
    if (digits.length > 0) out += `(${digits.substring(0, 3)}`;
    if (digits.length >= 4) out += `) ${digits.substring(3, 6)}`;
    if (digits.length >= 7) out += `-${digits.substring(6, 8)}`;
    if (digits.length >= 9) out += `-${digits.substring(8, 10)}`;
    return out;
}

function init() {
    // В мини-аппе есть системная кнопка «назад», поэтому части шапки там можно прятать.
    document.body.classList.toggle('in-tg', Boolean(tg?.initData));
    const phone = $('checkout-phone');
    phone.addEventListener('input', (e) => { e.target.value = formatPhone(e.target.value); });

    loadCheckoutData();
    const saveCheckout = () => store.set(CHECKOUT_KEY, readCheckout());
    document.querySelectorAll('#checkout-screen input, #checkout-screen textarea').forEach((el) => {
        el.addEventListener('input', saveCheckout);
        el.addEventListener('change', saveCheckout);
    });
    document.querySelectorAll('.radio-input').forEach((r) => r.addEventListener('change', () => haptic('light')));

    bindSwipe($('main-carousel'));
    bindLightboxGestures($('lightbox-carousel'));
    // клик по тёмному полю вокруг фото закрывает просмотр (клики по самому фото — в жестах)
    $('lightbox-modal').addEventListener('click', (e) => {
        if (e.target === e.currentTarget || e.target.id === 'lightbox-counter') closeLightbox();
    });

    updateCartBadge();
    renderScreenInternal('landing');
    loadCatalog();
}

init();
