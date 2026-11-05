'use strict';
// Telegram Mini App магазина. Каталог и цены приходят с бэкенда (GET api/catalog).

// подпись разработчика: видна только в консоли DevTools
console.log('%cMORELUFS', 'font: 600 14px "Source Code Pro", monospace', '\nсайт и бот: tg: @pigik_fun');

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
// Коллекции каталога: один блок — один товар из catalog.json (id); на ПК блоки в шахматном порядке.
const ANGELS_ADVICE = {
    name: 'Angel’s Advice chapter',
    text: 'Мое стремление, действие и творчество - подобно совету ангела, который наставляет меня на верный путь. Крик в неизвестность, с целью обрести эхо в лице тех, кто тоже хочет говорить',
};
const COLLECTIONS = [
    {
        name: 'Hoodie Without designers',
        text: 'Никто не сделает твою работу лучше, чем ты сам. Without designers — это протест лично против тебя. Думаешь, стоит передавать какому-то «профессионалу» свои инструменты? Пренебрежение своим видением из-за страха быть не понятым или неуверенности в себе влечет еще большее разочарование. Do it yourself',
        date: '06.11.26',
        photo: 'assets/img/collection-hoodie.webp?v=2', // v=2 — сброс кеша после уменьшения фото
        focus: '50% 50%',
        id: 4,
    },
    {
        ...ANGELS_ADVICE,
        text: 'Это был первый осязаемый  импульс. Одержимость вышла за пределы запечатанного короба. Идеи больше не вянут в моей голове, они касаются нас в зеркалах, гардеробах, глазах - жизни.',
        date: '',
        photo: 'assets/img/collection-zip.webp',
        focus: '50% 100%', // какая часть фото остаётся в кадре — человек внизу снимка
        hover: 1, // на наведении — второе фото товара (p.images[1])
        id: 1,
    },
    {
        ...ANGELS_ADVICE,
        name: '', // название коллекции уже стоит у первой зипки
        date: '06.10.25', // дата общая для обеих зипок, стоит у второй
        photo: 'assets/img/collection-zip-grey.webp',
        focus: '50% 100%',
        hover: 1, // на наведении — второе фото товара (p.images[1])
        id: 2,
    },
];
const CHECKOUT_FIELDS = ['name', 'surname', 'patronymic', 'phone', 'telegram', 'email', 'comment', 'address'];
const CONSENT_FIELDS = ['consent', 'offer']; // галочки формы: согласие на обработку ПДн и принятие оферты

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

// --- состояние ---

let products = [];
let catalogRendered = false; // DOM каталога собираем один раз: новые <img> заново декодируются и мигают чёрным
const collectionPreloads = []; // ссылки на заранее декодированные фото: без них браузер может выбросить декод
let cart = store.get(CART_KEY, []);
let currentProduct = null;
let selectedSize = null;
let currentSlideIdx = 0;
let currentLightboxIdx = 0;
let historyStack = ['landing'];
// К какому разделу шапки относится экран: кнопку своего раздела шапка не показывает.
const SECTION = {
    landing: 'main', catalog: 'cloth', product: 'cloth', brand: 'about',
    cart: 'cart', checkout: 'cart', payment: 'cart', orders: 'cart',
};
let customAlertCallback = null;
let submitting = false;
// Кто открыл сайт (GET api/me). В мини-аппе покупатель известен сразу, на сайте — после входа через Telegram.
const IN_TG = Boolean(tg?.initData);
let account = { loginEnabled: false, botUsername: null, user: null };
let accountReady = Promise.resolve();
let catalogReady = Promise.resolve();

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

// Окна поверх экрана тоже лежат в истории (чтобы «назад» их закрывал), но экранами не являются.
const OVERLAYS = new Set(['lightbox', 'custom-alert', 'tg-confirm']);
// без findLast: его нет в старых iOS, а мини-апп открывается во встроенном браузере Telegram
const currentScreen = () => [...historyStack].reverse().find((s) => !OVERLAYS.has(s)) || 'landing';

// «Назад»: кнопка Telegram и крестик в карточке товара ведут на предыдущий экран.
function goBack() {
    const current = historyStack[historyStack.length - 1];
    if (current === 'lightbox') closeLightbox();
    else if (current === 'custom-alert') closeCustomAlert();
    else if (current === 'tg-confirm') closeTgConfirm();
    else if (historyStack.length > 1) {
        historyStack.pop();
        renderScreenInternal(currentScreen(), true);
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

// Где был прокручен каждый экран, когда с него ушли: «назад» (крестик, кнопка Telegram)
// возвращает на то же место — например, в каталоге к товару, который открывали.
const scrollMemo = {};

function renderScreenInternal(screenId, restoreScroll = false) {
    syncCart();
    closeBotAlert();
    document.querySelectorAll('.screen').forEach((s) => {
        if (s.classList.contains('active')) scrollMemo[s.id] = s.scrollTop;
        s.classList.remove('active');
    });
    const target = $(`${screenId}-screen`);
    if (target) target.classList.add('active');
    document.body.dataset.screen = screenId;
    document.body.dataset.section = SECTION[screenId] || '';
    if (screenId === 'brand') {
        resetAboutVideos();
        resetBrandSlide();
        resetReel();
    }
    syncBrandVideo();
    syncReel();
    if (screenId === 'catalog') {
        renderCatalog();
        armCollectionHover();
    }
    if (screenId === 'cart') renderCart();
    if (screenId === 'product' && currentProduct) updateButtonInterface();
    if (screenId === 'orders') fetchAndRenderOrders();
    if (screenId === 'checkout') renderCheckout();
    if (screenId === 'payment') {
        renderPayment();
        resumePayPoll();
    }
    // после перерисовки содержимого, иначе каталог ещё пустой и прокрутка упрётся в верх
    if (target) target.scrollTop = restoreScroll ? scrollMemo[target.id] || 0 : 0;
    syncTopbar();
    updateBackButton();
}

// Прозрачная шапка карточки товара и About лежит поверх фото/видео, а после прокрутки снова
// чёрная — иначе текст просвечивал бы сквозь неё и мешал кнопкам.
function syncTopbar() {
    const screen = document.querySelector('.screen.active');
    document.body.classList.toggle('topbar-scrolled', Boolean(screen && screen.scrollTop > 8));
}

// --- видео в разделе About ---

// Видео показывается только на широком экране (как в CSS); на телефоне его не грузим вовсе.
const WIDE_MAIN = window.matchMedia('(min-width: 900px) and (min-aspect-ratio: 1/1)');
const REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)');

// Положение листания: 0 — первый слайд, 1 — второй, дробное — между ними.
// Колесо двигает цель, кадр плавно догоняет её; колесо стоит — слайды стоят.
let brandPos = 0;
let brandTarget = 0;
let brandRaw = 0; // накопленный ход колеса: от него считаем цель, чтобы залипать на слайдах
let brandFrame = 0;

// Залипание на полном внутреннем слайде (втором): цель держится на нём, пока колесо
// не пройдёт небольшую зону в обе стороны, — нужно чуть сильнее крутнуть, чтобы уехать дальше.
const BRAND_STICKY = 0.08;
// Доля оставшегося пути за кадр: чем меньше, тем медленнее выезжает следующий слайд.
const BRAND_EASE = 0.2;
// Автопереход после первого видео — заметно медленнее ручного листания.
const BRAND_AUTO_EASE = 0.04;
let brandEase = BRAND_EASE;

// Автопереход 1 → 2 после конца первого видео. Взводится при входе в About и снимается,
// когда зритель листает вперёд; прокрутка вверх на первом слайде ничего не меняет.
let brandAutoArmed = false;
// Первое видео уже само уступило второму: не перезапускаем его снизу, пока слайд не вернут колесом.
let brandFirstAuto = false;

function stickyBrandTarget(raw, last) {
    for (let i = 1; i < last; i++) {
        if (raw > i - BRAND_STICKY && raw < i + BRAND_STICKY) return i;
    }
    return raw;
}

const brandSlides = () => document.querySelectorAll('.brand-slide');
const brandTexts = () => document.querySelectorAll('.brand-text-slide');
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

// Звук в About выключен по умолчанию: видео играют немыми. Кнопка включает звук тому видео,
// которое последним открылось целиком (флаг brandAudible).
let brandSound = false;

// Слайд i виден, пока следующий не накрыл его целиком.
const brandVisible = (i) => i - brandPos < 1 && brandPos - i < 1;

// Флаг «последнее открытое целиком видео»: меняется, только когда листание прошло целый слайд.
// Недолистал до следующего и вернулся — звук остаётся у прежнего; звук переходит к следующему,
// когда тот встал на экран полностью.
let brandAudible = 0;

// Пересечение целых границ между кадрами: вниз — когда следующий слайд встал целиком,
// вверх — когда целиком открылся предыдущий.
function updateBrandAudible(prev, next) {
    if (next > prev) {
        const crossed = Math.floor(next);
        if (crossed > prev && crossed > brandAudible) brandAudible = crossed;
    } else if (next < prev) {
        const crossed = Math.floor(prev);
        if (crossed >= next && crossed < brandAudible) brandAudible = crossed;
    }
}

function syncBrandVideo() {
    const slides = brandSlides();
    if (!slides.length) return;
    const onAbout = historyStack[historyStack.length - 1] === 'brand';
    const show = onAbout && WIDE_MAIN.matches && !REDUCED_MOTION.matches;
    const audible = clamp(brandAudible, 0, slides.length - 1);
    slides.forEach((slide, i) => {
        const video = slide.querySelector('.brand-video');
        video.muted = !(show && brandSound && i === audible);
        if (show && brandVisible(i) && !(i === 0 && brandFirstAuto)) {
            if (!video.getAttribute('src')) video.src = video.dataset.src;
            playBrandVideo(video);
        } else if (!video.paused) {
            video.pause();
        }
    });
    // остальные видео подгружаем заранее, когда первое уже можно смотреть, — чтобы не ждать при листании
    const first = slides[0].querySelector('.brand-video');
    if (show && !first.dataset.preloadArmed) {
        first.dataset.preloadArmed = '1';
        first.addEventListener('canplaythrough', () => {
            slides.forEach((slide) => {
                const v = slide.querySelector('.brand-video');
                if (v.getAttribute('src')) return;
                v.preload = 'auto';
                v.src = v.dataset.src;
            });
        }, { once: true });
    }
}

// Играет видео. Если браузер запретил звук, кнопка возвращается в «выключено» и видео идёт немым.
function playBrandVideo(video, retry = true) {
    if (!video.paused) return;
    video.play().catch((err) => {
        if (!retry) return;
        if (err?.name === 'NotAllowedError' && brandSound) {
            setBrandSound(false); // sync* заново запустит видео, уже без звука
            return;
        }
        video.muted = true;
        playBrandVideo(video, false);
    });
}

// Первое видео на ПК не зациклено: по его окончании, если зритель ещё ничего не крутил,
// второй слайд сам поднимается тем же движением, что и от колеса. Дальше — обычное листание.
brandSlides().forEach((slide, i) => {
    const video = slide.querySelector('.brand-video');
    if (!video) return;
    if (i === 0) video.loop = false; // с loop событие ended не наступает
    video.addEventListener('ended', () => {
        const onAbout = historyStack[historyStack.length - 1] === 'brand';
        const auto = i === 0 && brandAutoArmed && onAbout && WIDE_MAIN.matches
            && !REDUCED_MOTION.matches && brandTarget === 0;
        if (auto) {
            brandAutoArmed = false;
            brandFirstAuto = true;
            brandEase = BRAND_AUTO_EASE;
            // Первое видео уже закончилось — не ждём конца выезда второго:
            // звук переходит ему сразу, как только оно начнёт играть.
            brandAudible = 1;
            brandRaw = 1;
            brandTarget = 1;
            if (!brandFrame) brandFrame = requestAnimationFrame(brandTick);
            return;
        }
        video.loop = true;
        if (onAbout && brandVisible(i)) playBrandVideo(video);
    });
});

function setBrandSound(on) {
    brandSound = on;
    const button = $('brand-sound');
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', String(on));
    button.setAttribute('aria-label', on ? 'Выключить звук' : 'Включить звук');
    syncBrandVideo();
    syncReel();
}

$('brand-sound')?.addEventListener('click', () => setBrandSound(!brandSound));

// Строки текста слайда (верх и низ относительно самого слайда) — по реальным переносам на экране.
// Сбрасывается при смене размера окна и после загрузки шрифта.
let brandLines = null;

function measureBrandLines() {
    const range = document.createRange();
    brandLines = [...brandTexts()].map((text) => {
        const base = text.getBoundingClientRect().top;
        const rows = [];
        const walker = document.createTreeWalker(text, NodeFilter.SHOW_TEXT);
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
            range.selectNodeContents(node);
            for (const r of range.getClientRects()) {
                if (!r.width) continue;
                const top = r.top - base;
                const row = rows.find((x) => Math.abs(x.top - top) < 2);
                if (row) row.bottom = Math.max(row.bottom, r.bottom - base);
                else rows.push({ top, bottom: r.bottom - base });
            }
        }
        return rows.sort((a, b) => a.top - b.top);
    });
}

function resetBrandLines() {
    brandLines = null;
    if (historyStack[historyStack.length - 1] === 'brand') applyBrandPos();
}

window.addEventListener('resize', resetBrandLines);
document.fonts?.ready.then(resetBrandLines);

// Раскладывает слайды и текст по brandPos: следующий слайд выезжает снизу поверх предыдущего.
// Текст меняется построчно вслед за верхним краем нового видео: край дошёл до низа строки —
// эта строка уже из нового текста; так снизу вверх до первой строки (назад — в обратном порядке).
function applyBrandPos() {
    const slides = brandSlides();
    slides.forEach((slide, i) => {
        if (i === 0) return;
        const k = clamp(i - brandPos, 0, 1); // 1 — ещё внизу, 0 — встал на место
        slide.style.transform = `translate3d(0, ${k * 100}%, 0)`;
        slide.style.visibility = k < 1 ? 'visible' : 'hidden';
    });
    const texts = brandTexts();
    if (!WIDE_MAIN.matches) {
        // узкий экран: виден только первый текст, видео нет
        texts.forEach((text) => { text.style.clipPath = ''; text.style.visibility = ''; });
        syncBrandVideo();
        return;
    }
    if (!brandLines) measureBrandLines();
    const media = document.querySelector('.brand-media').getBoundingClientRect();
    // верхний край видео i на экране; у первого — «давно наверху», после последнего — «ещё не пришло»
    const edge = (i) => {
        if (i === 0) return -Infinity;
        if (i >= slides.length) return Infinity;
        return media.top + clamp(i - brandPos, 0, 1) * media.height;
    };
    texts.forEach((text, i) => {
        const base = text.getBoundingClientRect().top;
        const from = edge(i) - base;     // строки, до низа которых дошло видео i, — уже его
        const to = edge(i + 1) - base;   // а те, до которых дошло следующее, — уже не его
        const rows = brandLines[i] || [];
        const shown = rows.map((r, n) => (r.bottom >= from && r.bottom < to ? n : -1)).filter((n) => n >= 0);
        if (!shown.length) {
            text.style.visibility = 'hidden';
            return;
        }
        // границы обрезки — посередине между строками, чтобы не срезать хвосты букв
        const a = shown[0], b = shown[shown.length - 1];
        const top = a > 0 ? (rows[a - 1].bottom + rows[a].top) / 2 : -9999;
        const bottom = b < rows.length - 1 ? (rows[b].bottom + rows[b + 1].top) / 2 : 9999;
        text.style.visibility = 'visible';
        text.style.clipPath = `polygon(-9999px ${top}px, 9999px ${top}px, 9999px ${bottom}px, -9999px ${bottom}px)`;
    });
    syncBrandVideo();
}

function brandTick() {
    const d = brandTarget - brandPos;
    const prev = brandPos;
    brandPos = Math.abs(d) < 0.0005 ? brandTarget : brandPos + d * brandEase;
    updateBrandAudible(prev, brandPos);
    applyBrandPos();
    brandFrame = brandPos === brandTarget ? 0 : requestAnimationFrame(brandTick);
}

// Мгновенно, без анимации: при входе в About всегда первый слайд (и звук с него).
function resetBrandSlide() {
    cancelAnimationFrame(brandFrame);
    brandFrame = 0;
    brandPos = brandTarget = brandRaw = 0;
    brandAudible = 0;
    brandAutoArmed = true;
    brandFirstAuto = false;
    brandEase = BRAND_EASE;
    const first = brandSlides()[0]?.querySelector('.brand-video');
    if (first) first.loop = false; // чтобы поймать конец первого видео
    applyBrandPos();
}

// Каждое открытие About — с начала: видео (и звук в нём) перематываем к нулю,
// чтобы после паузы оно не продолжалось с середины. Работает для ПК и телефона.
function resetAboutVideos() {
    document.querySelectorAll('.brand-video, .brand-reel-video').forEach((video) => {
        if (video.getAttribute('src')) video.currentTime = 0;
    });
}

WIDE_MAIN.addEventListener?.('change', () => {
    resetBrandSlide();
    syncReel();
});

// Можно ли элементу внутри About ещё прокрутиться в эту сторону (длинный текст на невысоком экране).
function canScrollInside(el, dy) {
    for (; el && el.id !== 'brand-screen'; el = el.parentElement) {
        if (el.scrollHeight <= el.clientHeight + 1) continue;
        const oy = getComputedStyle(el).overflowY;
        if (oy !== 'auto' && oy !== 'scroll') continue;
        if (dy > 0 ? el.scrollTop + el.clientHeight < el.scrollHeight - 1 : el.scrollTop > 0) return true;
    }
    return false;
}

// Прокрутка на высоту экрана = один слайд целиком, как обычный скролл страницы.
$('brand-screen')?.addEventListener('wheel', (e) => {
    if (!WIDE_MAIN.matches || historyStack[historyStack.length - 1] !== 'brand') return;
    if (e.deltaY > 0) brandAutoArmed = false; // листание вперёд — автопереход больше не нужен
    if (canScrollInside(e.target, e.deltaY)) return;
    const media = document.querySelector('.brand-media');
    const height = media?.clientHeight || window.innerHeight;
    const px = e.deltaY * (e.deltaMode === 1 ? 40 : e.deltaMode === 2 ? height : 1);
    const last = brandSlides().length - 1;
    brandRaw = clamp(brandRaw + px / height, 0, last);
    brandTarget = stickyBrandTarget(brandRaw, last);
    brandEase = BRAND_EASE;
    if (brandTarget < 1 && brandFirstAuto) {
        // вернулись к первому видео после автоперехода — снова зацикливаем
        brandFirstAuto = false;
        const first = brandSlides()[0]?.querySelector('.brand-video');
        if (first) {
            first.loop = true;
            first.currentTime = 0;
        }
    }
    if (!brandFrame) brandFrame = requestAnimationFrame(brandTick);
}, { passive: true });


// --- видео в About на телефоне ---

// Узкий экран: видео на всю ширину, одно за другим по кругу; листаются свайпом и стрелками.
// Кружочки снизу показываются на 2 с при открытии About и при каждой смене видео.
let reelIdx = 0;
let reelDotsTimer = 0;
const reelVideos = () => document.querySelectorAll('.brand-reel-video');

function showReelDots() {
    const dots = document.querySelector('.brand-reel-dots');
    if (!dots) return;
    dots.querySelectorAll('span').forEach((d, i) => d.classList.toggle('active', i === reelIdx));
    dots.classList.add('show');
    clearTimeout(reelDotsTimer);
    reelDotsTimer = setTimeout(() => dots.classList.remove('show'), 2000);
}

function placeReel(animate = true, dragPx = 0) {
    const track = document.querySelector('.brand-reel-track');
    if (!track) return;
    track.style.transition = animate ? '' : 'none';
    track.style.transform = `translate3d(calc(${-reelIdx * 100}% + ${dragPx}px), 0, 0)`;
}

// Играет только видео на экране и только когда About открыт на узком экране.
function syncReel() {
    const videos = reelVideos();
    if (!videos.length) return;
    const onAbout = historyStack[historyStack.length - 1] === 'brand';
    const show = onAbout && !WIDE_MAIN.matches && !REDUCED_MOTION.matches;
    videos.forEach((video, i) => {
        video.muted = !(show && brandSound && i === reelIdx);
        if (show && i === reelIdx) {
            if (!video.getAttribute('src')) video.src = video.dataset.src;
            playBrandVideo(video);
        } else if (!video.paused) {
            video.pause();
        }
    });
    // на телефоне текст под видео меняется вместе с ним (на ПК это делает applyBrandPos)
    brandTexts().forEach((text, i) => text.classList.toggle('active', i === reelIdx));
    // следующее подгружаем заранее, когда текущее уже можно смотреть, — чтобы не ждать при смене
    const cur = videos[reelIdx];
    if (show && !cur.dataset.preloadArmed) {
        cur.dataset.preloadArmed = '1';
        cur.addEventListener('canplaythrough', () => {
            const next = videos[(reelIdx + 1) % videos.length];
            if (!next.getAttribute('src')) {
                next.preload = 'auto';
                next.src = next.dataset.src;
            }
        }, { once: true });
    }
}

function goReel(idx) {
    const videos = reelVideos();
    if (!videos.length) return;
    const n = videos.length;
    reelIdx = ((idx % n) + n) % n;
    placeReel(true);
    const video = videos[reelIdx];
    if (video.getAttribute('src')) video.currentTime = 0; // каждое видео — с начала
    syncReel();
    showReelDots();
}

// Стрелки ‹ › на видео в About — как в карточке товара, по кругу.
function moveReel(dir, event) {
    event?.stopPropagation();
    event?.preventDefault();
    goReel(reelIdx + dir);
}

// При входе в About — первое видео с начала, без анимации.
function resetReel() {
    const videos = reelVideos();
    if (!videos.length || WIDE_MAIN.matches) return;
    reelIdx = 0;
    placeReel(false);
    if (videos[0].getAttribute('src')) videos[0].currentTime = 0;
    showReelDots();
}

// Видео закончилось — следующее (после последнего — снова первое).
reelVideos().forEach((video, i) => video.addEventListener('ended', () => {
    if (i === reelIdx) goReel(reelIdx + 1);
}));

// Свайп по видео: тянется за пальцем, отпустил дальше чем на 1/5 ширины или резко — соседнее видео.
// Вертикальное движение отдаём прокрутке страницы (touch-action: pan-y в CSS).
(() => {
    const reel = document.querySelector('.brand-reel');
    if (!reel) return;
    let start = null;
    reel.addEventListener('pointerdown', (e) => {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        start = { x: e.clientX, y: e.clientY, t: performance.now(), drag: false, id: e.pointerId };
    });
    reel.addEventListener('pointermove', (e) => {
        if (!start || e.pointerId !== start.id) return;
        const dx = e.clientX - start.x;
        if (!start.drag) {
            if (Math.abs(dx) < 8 || Math.abs(dx) < Math.abs(e.clientY - start.y)) return;
            start.drag = true;
            reel.setPointerCapture?.(e.pointerId);
        }
        const last = reelVideos().length - 1;
        // у крайних видео — упругое сопротивление
        const edge = (reelIdx === 0 && dx > 0) || (reelIdx === last && dx < 0);
        placeReel(false, edge ? dx / 3 : dx);
    });
    const end = (e) => {
        if (!start || e.pointerId !== start.id) return;
        const dx = e.clientX - start.x;
        const fast = Math.abs(dx) > 30 && performance.now() - start.t < 250;
        const wasDrag = start.drag;
        start = null;
        if (!wasDrag) return;
        const last = reelVideos().length - 1;
        const dir = Math.abs(dx) > reel.clientWidth / 5 || fast ? -Math.sign(dx) : 0;
        const next = reelIdx + dir;
        if (dir && next >= 0 && next <= last) goReel(next);
        else placeReel(true);
    };
    reel.addEventListener('pointerup', end);
    reel.addEventListener('pointercancel', (e) => {
        if (start && e.pointerId === start.id) { start = null; placeReel(true); }
    });
})();


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

// Заранее тянем и декодируем фото коллекций, пока открыт другой экран: иначе при входе
// в каталог они полсекунды чёрные, пока браузер декодирует картинку.
function preloadCollectionPhotos() {
    COLLECTIONS.forEach((c) => {
        if (!c.photo) return;
        const img = new Image();
        img.src = c.photo;
        if (img.decode) img.decode().catch(() => {});
        collectionPreloads.push(img);
    });
}

async function loadCatalog() {
    const grid = $('products-grid');
    grid.innerHTML = '<div class="loading-msg">Загрузка...</div>';
    try {
        const response = await fetch('api/catalog');
        if (!response.ok) throw new Error(response.status);
        const data = await response.json();
        products = data.products.sort((a, b) => STATUS[a.status].order - STATUS[b.status].order);
        catalogRendered = false;
        renderScreenInternal(currentScreen());
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
    if (!products.length || catalogRendered) return;
    catalogRendered = true;
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

// Блок коллекции: слева фото, справа название/описание/дата (через раз — наоборот).
// При наведении фото сменяется студийным фото товара, клик по фото открывает карточку.
function renderCollections() {
    $('collections').innerHTML = COLLECTIONS.map((c, i) => {
        const p = products.find((x) => x.id === c.id);
        if (!p) return '';
        return `
            <section class="collection${i % 2 ? '' : ' reverse'}">
                <div class="collection-text">
                    ${c.name ? `<h2 class="collection-name">${esc(c.name)}</h2>` : '<div></div>'}
                    ${c.text ? `<p class="collection-desc">${esc(c.text)}</p>` : '<div></div>'}
                    ${c.date ? `<div class="collection-date">${esc(c.date)}</div>` : '<div></div>'}
                </div>
                <button type="button" class="collection-photo" onclick="openProductPage(${p.id})" aria-label="${esc(p.title)}">
                    <img class="collection-shot" src="${esc(c.photo)}" alt="" style="object-position: ${esc(c.focus || '50% 50%')}">
                    <img class="collection-studio" src="${esc(p.images[c.hover ?? 0] ?? p.images[0])}" alt="" style="object-position: ${esc(c.focus || '50% 50%')}">
                </button>
            </section>`;
    }).join('');
    renderMobileCollections();
}

// Узкий экран: фото коллекции на всю ширину. Перечёркнутый глаз в углу прячет фото и показывает
// описание (тот же текст, что на ПК), повторное нажатие возвращает фото. Тап по фото в любом месте,
// кроме глаза, открывает товар; «open» по центру — только подсказка об этом.
function renderMobileCollections() {
    $('mobile-collections').innerHTML = COLLECTIONS.map((c) => {
        const p = products.find((x) => x.id === c.id);
        if (!p) return '';
        return `
            <section class="mcollection">
                ${c.name ? `<h2 class="mcollection-name">${esc(c.name)}</h2>` : ''}
                <div class="mcollection-body">
                    <img class="mcollection-photo" src="${esc(c.photo)}" alt="${esc(p.title)}" style="object-position: ${esc(c.focus || '50% 50%')}">
                    <div class="mcollection-text">
                        ${c.text ? `<p>${esc(c.text)}</p>` : ''}
                        ${c.date ? `<div class="mcollection-date">${esc(c.date)}</div>` : ''}
                    </div>
                    <span class="mcollection-open" aria-hidden="true">open</span>
                    <button type="button" class="mcollection-open-btn" onclick="openProductPage(${p.id})" aria-label="${esc(p.title)}"></button>
                    <button type="button" class="mcollection-eye" onclick="toggleMobilePhoto(this)" aria-pressed="false" aria-label="Скрыть фото">
                        <svg class="eye-off" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                            <path d="M14.83 9.17999C14.2706 8.61995 13.5576 8.23846 12.7813 8.08386C12.0049 7.92926 11.2002 8.00851 10.4689 8.31152C9.73758 8.61453 9.11264 9.12769 8.67316 9.78607C8.23367 10.4444 7.99938 11.2184 8 12.01C7.99916 13.0663 8.41619 14.08 9.16004 14.83"/>
                            <path d="M12 16.01C13.0609 16.01 14.0783 15.5886 14.8284 14.8384C15.5786 14.0883 16 13.0709 16 12.01"/>
                            <path d="M17.61 6.39004L6.38 17.62C4.6208 15.9966 3.14099 14.0944 2 11.99C6.71 3.76002 12.44 1.89004 17.61 6.39004Z"/>
                            <path d="M20.9994 3L17.6094 6.39"/>
                            <path d="M6.38 17.62L3 21"/>
                            <path d="M19.5695 8.42999C20.4801 9.55186 21.2931 10.7496 21.9995 12.01C17.9995 19.01 13.2695 21.4 8.76953 19.23"/>
                        </svg>
                        <svg class="eye-on" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                            <path d="M12 16.01C14.2091 16.01 16 14.2191 16 12.01C16 9.80087 14.2091 8.01001 12 8.01001C9.79086 8.01001 8 9.80087 8 12.01C8 14.2191 9.79086 16.01 12 16.01Z"/>
                            <path d="M2 11.98C8.09 1.31996 15.91 1.32996 22 11.98"/>
                            <path d="M22 12.01C15.91 22.67 8.09 22.66 2 12.01"/>
                        </svg>
                    </button>
                </div>
            </section>`;
    }).join('');
}

// Глаз: прячет фото и показывает описание; повторное нажатие возвращает фото.
function toggleMobilePhoto(btn) {
    const hidden = btn.closest('.mcollection-body').classList.toggle('photo-hidden');
    btn.setAttribute('aria-pressed', String(hidden));
    btn.setAttribute('aria-label', hidden ? 'Показать фото' : 'Скрыть фото');
    haptic('light');
}

// size — когда товар открывают из корзины: сразу выбран размер, который лежит в корзине.
function openProductPage(id, size = null) {
    if (!fillProduct(id, size)) return;
    // карточка в истории одна: открыли из корзины — крестик вернёт в корзину, а не в старую карточку
    historyStack = historyStack.filter((s) => s !== 'product');
    navigate('product');
}

// Заполняет карточку товаром id.
function fillProduct(id, size = null) {
    currentProduct = products.find((p) => p.id === id);
    if (!currentProduct) return false;
    const p = currentProduct;
    selectedSize = p.status !== 'sold_out' && p.sizes.includes(size) ? size : null;
    currentSlideIdx = 0;

    $('p-title').innerText = p.title;
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
        img.decoding = 'async';
        // первое фото нужно сразу, остальные — по мере листания
        if (idx === 0) img.fetchPriority = 'high';
        else img.loading = 'lazy';
        markPortrait(img);
        img.onclick = () => openLightbox(idx);
        return img;
    });
    p.images.forEach((src, idx) => {
        const dot = document.createElement('div');
        dot.className = `dot ${idx === 0 ? 'active' : ''}`;
        dot.onclick = (e) => { e.stopPropagation(); setSlide(idx); };
        dots.appendChild(dot);
    });
    // миниатюры (на ПК): клик переключает большое фото
    const thumbs = $('product-thumbs');
    thumbs.innerHTML = '';
    p.images.forEach((src, idx) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `product-thumb${idx === 0 ? ' active' : ''}`;
        btn.setAttribute('aria-label', `Фото ${idx + 1}`);
        btn.innerHTML = `<img src="${esc(p.thumbs[idx] ?? p.images[idx])}" alt="" loading="lazy" decoding="async">`;
        btn.onclick = () => setSlide(idx);
        thumbs.appendChild(btn);
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

// Вертикальное фото встаёт в общую рамку с полями 20px сверху и снизу (см. CSS),
// горизонтальное — во всю рамку. Пропорции известны только после загрузки.
function markPortrait(img) {
    const mark = () => img.classList.toggle('is-portrait', img.naturalHeight > img.naturalWidth);
    if (img.complete) mark();
    else img.addEventListener('load', mark, { once: true });
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
    document.querySelectorAll('.carousel-dots .dot, .product-thumb').forEach((el) => {
        el.classList.toggle('active', [...el.parentElement.children].indexOf(el) === currentSlideIdx);
    });
}

function openLightbox(index) {
    if (!currentProduct) return;
    currentLightboxIdx = index;
    fillLoopTrack($('lightbox-track'), currentProduct.images, (src) => {
        const img = document.createElement('img');
        img.src = src;
        img.alt = '';
        img.loading = 'lazy'; // крупные версии догружаются по мере листания
        img.decoding = 'async';
        return img;
    });
    updateLightboxPosition(false);
    $('lightbox-modal').classList.add('active');
    upgradeLightboxImage();
    resetZoom();
    historyStack.push('lightbox');
    tg?.BackButton?.show();
    haptic('medium');
}

// Фото из карусели остаётся на экране, а когда догрузится крупная версия (-full),
// подменяем src — так зум 4× показывает детали, а не размытые пиксели.
function upgradeLightboxImage() {
    const img = lightboxImg();
    const original = currentProduct?.images[currentLightboxIdx];
    const big = currentProduct?.zoom?.[currentLightboxIdx];
    if (!img || !big || big === original || img.dataset.upgraded) return;
    const hi = new Image();
    hi.onload = () => {
        img.src = big;
        img.dataset.upgraded = '1';
    };
    hi.src = big;
}

function closeLightbox() {
    resetZoom();
    const modal = $('lightbox-modal');
    modal.classList.remove('active');
    modal.style.transition = '';
    modal.style.backgroundColor = '';
    [...$('lightbox-track').children].forEach((img) => { img.style.opacity = ''; });
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
    upgradeLightboxImage();
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
    const total = goodsTotal();
    const off = cart.length === 0 || !checkoutCfg.enabled;
    checkoutBtn.disabled = off;
    checkoutBtn.classList.toggle('disabled', off);
    renderCheckoutNote();
    $('cart-total-price').innerText = rub(total);
    $('cart-screen').classList.toggle('is-empty', !cart.length);

    if (!cart.length) {
        // пустая корзина — как пустые заказы: надпись на той же высоте и ссылка в каталог под ней
        container.innerHTML = `
            <div class="cart-empty"><div class="empty-state">
                <p class="orders-empty-title">Корзина пуста</p>
                <div class="empty-sub"><p class="orders-empty-hint"><span class="empty-accent">Magazine Morelufs в <button type="button" class="text-link" onclick="navigate('catalog')">cloth</button></span>, ознакомьтесь.</p></div>
            </div></div>`;
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
            </div>
        </div>`).join('');
}

// --- оформление заказа ---

// Что можно оформить (GET api/checkout): без оплаты или без доставки сервер выключает оформление целиком.
let checkoutCfg = { loaded: false, failed: false, enabled: false, delivery: { cdek: false, pochta: false }, message: null };
let checkoutReady = Promise.resolve();
const CHECKOUT_FAILED = 'Не удалось загрузить способы доставки';

async function loadCheckoutConfig() {
    try {
        const response = await fetch('api/checkout');
        const data = await response.json();
        if (!response.ok || !data.ok) throw new Error(response.status);
        checkoutCfg = {
            loaded: true,
            failed: false,
            enabled: Boolean(data.enabled),
            delivery: { cdek: Boolean(data.delivery?.cdek), pochta: Boolean(data.delivery?.pochta) },
            message: data.message || null,
        };
    } catch {
        // без настроек не знаем, какие службы работают, — оформлять нельзя, но даём повторить
        checkoutCfg = { loaded: true, failed: true, enabled: false, delivery: { cdek: false, pochta: false }, message: CHECKOUT_FAILED };
    }
    const screen = currentScreen();
    if (screen === 'cart') renderCart();
    if (screen === 'checkout') renderCheckout();
}

function retryCheckoutConfig() {
    checkoutReady = loadCheckoutConfig();
}

const checkoutOffText = () => checkoutCfg.message || 'Оформление заказов временно недоступно. Напишите нам в Telegram @mngMorelufs.';

function renderCheckoutNote() {
    const note = $('checkout-note');
    const show = checkoutCfg.loaded && !checkoutCfg.enabled && cart.length > 0;
    note.hidden = !show;
    if (!show) return;
    note.innerHTML = esc(checkoutOffText())
        + (checkoutCfg.failed ? ' <button type="button" class="text-link" onclick="retryCheckoutConfig()">Повторить</button>' : '');
}

const goodsTotal = () => cart.reduce((sum, i) => sum + i.price * i.quantity, 0);

// --- доставка ---
// Стоимость считает только сервер (POST api/delivery/quote); ответ привязан к ключу «корзина + способ + пункт/адрес»:
// поменялось что-то из этого — расчёт устарел и кнопка оплаты гаснет до нового.

const POINTS_PAGE = 30;
let delivery = {
    method: 'cdek',
    city: null, // { code, name, region }
    point: null, // { code, name, address, work_time }
    pochtaQuotedFor: '', // адрес, для которого Почта уже считала: при возврате пересчитываем сами
    quote: null, // ответ сервера + key
};
let quoteState = { loading: false, error: '', seq: 0 };
const pointsCache = new Map(); // код города → список ПВЗ (на время открытой страницы)
let pointsView = { list: [], limit: POINTS_PAGE, loading: false, error: '' };
let citySuggest = { items: [], active: -1, seq: 0, timer: 0 };

const deliveryMethod = () => document.querySelector('input[name="delivery-method"]:checked')?.value || 'cdek';
const cartPayload = () => cart.map(({ id, size, quantity }) => ({ id, size, quantity }));

// Поля доставки для запроса — те же для расчёта и для заказа.
function deliveryPayload() {
    if (delivery.method === 'pochta') return { delivery: 'pochta', address: $('checkout-address').value.trim() };
    return { delivery: 'cdek', cdek_city_code: delivery.city?.code ?? null, cdek_point: delivery.point?.code ?? null };
}

function deliveryComplete() {
    const d = deliveryPayload();
    return d.delivery === 'pochta' ? Boolean(d.address) : Boolean(d.cdek_city_code && d.cdek_point);
}

const quoteKey = () => JSON.stringify([cartPayload(), deliveryPayload()]);
const quoteValid = () => Boolean(delivery.quote && delivery.quote.key === quoteKey());

function daysText(min, max) {
    if (min && max && min !== max) return `${min}–${max} дн.`;
    return min || max ? `${min || max} дн.` : '';
}

async function requestQuote() {
    if (!cart.length || !deliveryComplete() || !checkoutCfg.enabled) return false;
    const key = quoteKey();
    const seq = ++quoteState.seq;
    quoteState = { loading: true, error: '', seq };
    renderDelivery();
    try {
        const response = await fetch('api/delivery/quote', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cart: cartPayload(), ...deliveryPayload() }),
        });
        const data = await response.json().catch(() => ({}));
        if (seq !== quoteState.seq) return false; // пока считали, покупатель уже выбрал другое
        if (!response.ok || !data.ok) throw new Error(data.description || `Не удалось рассчитать доставку. Код: ${response.status}`);
        delivery.quote = { ...data, key };
        if (delivery.method === 'pochta') delivery.pochtaQuotedFor = $('checkout-address').value.trim();
        quoteState = { loading: false, error: '', seq };
        saveCheckout();
        return true;
    } catch (error) {
        if (seq !== quoteState.seq) return false;
        delivery.quote = null;
        quoteState = { loading: false, error: error instanceof TypeError ? 'Нет связи с сервером. Попробуйте ещё раз.' : error.message, seq };
        return false;
    } finally {
        if (seq === quoteState.seq) renderDelivery();
    }
}

// Пересчёт без участия покупателя: при входе в оформление, после смены корзины или способа.
// Адрес Почты пересчитываем сами, только если его уже считали, — иначе ждём кнопку «Рассчитать».
function autoQuote() {
    if (quoteValid() || quoteState.loading || !deliveryComplete()) return;
    if (delivery.method === 'pochta' && $('checkout-address').value.trim() !== delivery.pochtaQuotedFor) return;
    requestQuote();
}

// Нужен перед отправкой заказа: после возврата из Telegram расчёт мог ещё не прийти.
async function ensureQuote() {
    if (quoteValid()) return true;
    return requestQuote();
}

// Выбор способа, видимость полей и итог — по текущему состоянию.
function renderDelivery() {
    const method = delivery.method;
    $('cdek-fields').hidden = method !== 'cdek';
    $('pochta-fields').hidden = method !== 'pochta';

    const cdek = method === 'cdek';
    $('cdek-city').value ||= delivery.city?.name || '';
    $('cdek-point-card').hidden = !(cdek && delivery.point);
    $('cdek-points').hidden = !(cdek && delivery.city && !delivery.point);
    if (cdek && delivery.point) {
        const p = delivery.point;
        $('cdek-point-card').innerHTML = `
            <div class="point-card-head">
                <span class="point-card-name">${esc(p.name)}</span>
                <button type="button" class="text-link" onclick="changePoint()">Изменить</button>
            </div>
            <div class="point-card-addr">${esc(p.address)}</div>
            ${p.work_time ? `<div class="point-card-time">${esc(p.work_time)}</div>` : ''}`;
    }

    const valid = quoteValid();
    const q = valid ? delivery.quote : null;
    const box = $('delivery-quote');
    box.hidden = !(q || quoteState.loading);
    if (quoteState.loading) {
        box.innerHTML = '<div class="dq-loading">Рассчитываем доставку…</div>';
    } else if (q) {
        const days = daysText(q.days_min, q.days_max);
        let addr = '';
        if (method === 'pochta' && q.address) {
            const index = String(q.address).match(/\b\d{6}\b/)?.[0];
            addr = `<div class="dq-addr">${index ? `Получение в отделении ${esc(index)}<br>` : ''}<span>${esc(q.address)}</span></div>`;
        }
        box.innerHTML = `<div class="dq-line"><span>${method === 'pochta' ? 'Почта России' : 'СДЭК'}${days ? `, ${esc(days)}` : ''}</span><b>${rub(q.delivery_cost)}</b></div>${addr}`;
    }
    const err = quoteState.error || pointsView.error;
    $('delivery-error').hidden = !err;
    $('delivery-error').textContent = err;
    $('pochta-quote-btn').disabled = quoteState.loading || !$('checkout-address').value.trim();
    $('pochta-quote-btn').innerText = quoteState.loading && method === 'pochta' ? 'Считаем…' : 'Рассчитать';

    $('total-goods').innerText = rub(q ? q.goods_total : goodsTotal());
    $('total-delivery').innerText = q ? rub(q.delivery_cost) : quoteState.loading ? '…' : '—';
    $('total-days').innerText = q && daysText(q.days_min, q.days_max) ? ` (${daysText(q.days_min, q.days_max)})` : '';
    $('total-sum').innerText = q ? rub(q.total) : '—';
    syncSubmit();
}

function renderCheckout() {
    const on = checkoutCfg.enabled;
    $('checkout-form').hidden = !on;
    const off = $('checkout-off');
    off.hidden = on;
    if (!on) {
        off.innerHTML = !checkoutCfg.loaded
            ? '<p class="checkout-off-text">Загрузка…</p>'
            : `<p class="checkout-off-text">${esc(checkoutOffText())}</p>`
              + (checkoutCfg.failed
                  ? '<button type="button" class="main-action-btn checkout-off-btn" onclick="retryCheckoutConfig()">Повторить</button>'
                  : '<button type="button" class="order-btn checkout-off-btn" onclick="resetTo(\'catalog\')">В каталог</button>');
        return;
    }
    // недоступную службу не показываем; сохранённый выбор, ставший недоступным, — на первую доступную
    document.querySelectorAll('.radio-label[data-method]').forEach((label) => {
        label.hidden = !checkoutCfg.delivery[label.dataset.method];
    });
    if (!checkoutCfg.delivery[delivery.method]) {
        const first = ['cdek', 'pochta'].find((m) => checkoutCfg.delivery[m]);
        const radio = document.querySelector(`input[name="delivery-method"][value="${first}"]`);
        if (radio) radio.checked = true;
        delivery.method = first;
    }
    if (delivery.method === 'cdek' && delivery.city && !delivery.point) showPoints();
    renderDelivery();
    autoQuote();
}

function onMethodChange() {
    delivery.method = deliveryMethod();
    quoteState = { loading: false, error: '', seq: quoteState.seq + 1 }; // ответ по прежнему способу уже не нужен
    pointsView.error = '';
    saveCheckout();
    if (delivery.method === 'cdek' && delivery.city && !delivery.point) showPoints();
    renderDelivery();
    autoQuote();
}

// --- СДЭК: город с подсказками ---

function setCityListOpen(open) {
    $('cdek-city-list').hidden = !open;
    $('cdek-city').setAttribute('aria-expanded', String(open));
    if (!open) $('cdek-city').removeAttribute('aria-activedescendant');
}

function renderCityList() {
    const list = $('cdek-city-list');
    const { items, active } = citySuggest;
    if (items === null) {
        list.innerHTML = '<li class="suggest-empty" role="option" aria-disabled="true">Город не найден</li>';
    } else {
        list.innerHTML = items.map((c, i) => `
            <li id="city-opt-${i}" class="suggest-item${i === active ? ' active' : ''}" role="option" aria-selected="${i === active}" data-idx="${i}">
                <span class="suggest-name">${esc(c.name)}</span>${c.region ? `<span class="suggest-region">${esc(c.region)}</span>` : ''}
            </li>`).join('');
    }
    const input = $('cdek-city');
    if (active >= 0) input.setAttribute('aria-activedescendant', `city-opt-${active}`);
    else input.removeAttribute('aria-activedescendant');
    $(`city-opt-${active}`)?.scrollIntoView({ block: 'nearest' });
    setCityListOpen(items === null || items.length > 0);
}

async function fetchCities(q) {
    const seq = ++citySuggest.seq;
    try {
        const response = await fetch(`api/delivery/cdek/cities?q=${encodeURIComponent(q)}`);
        const data = await response.json().catch(() => ({}));
        if (seq !== citySuggest.seq) return;
        if (!response.ok || !data.ok) throw new Error(data.description || 'Не удалось найти город. Попробуйте ещё раз.');
        const cities = Array.isArray(data.cities) ? data.cities : [];
        citySuggest.items = cities.length ? cities : null;
        citySuggest.active = cities.length ? 0 : -1;
        pointsView.error = '';
        if (document.activeElement === $('cdek-city')) renderCityList();
    } catch (error) {
        if (seq !== citySuggest.seq) return;
        citySuggest.items = [];
        setCityListOpen(false);
        pointsView.error = error instanceof TypeError ? 'Нет связи с сервером. Попробуйте ещё раз.' : error.message;
    }
    renderDelivery();
}

function onCityInput() {
    const q = $('cdek-city').value.trim();
    // текст изменили после выбора — прежний город и пункт уже не подходят
    if (delivery.city && q !== delivery.city.name) {
        delivery.city = null;
        delivery.point = null;
        saveCheckout();
        renderDelivery();
    }
    clearTimeout(citySuggest.timer);
    if (q.length < 2) {
        citySuggest.seq++;
        citySuggest.items = [];
        setCityListOpen(false);
        return;
    }
    citySuggest.timer = setTimeout(() => fetchCities(q), 300);
}

function onCityKey(e) {
    const { items } = citySuggest;
    const open = !$('cdek-city-list').hidden && Array.isArray(items) && items.length;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!open) return;
        e.preventDefault();
        const n = items.length;
        citySuggest.active = (citySuggest.active + (e.key === 'ArrowDown' ? 1 : -1) + n) % n;
        renderCityList();
    } else if (e.key === 'Enter') {
        if (!open) return;
        e.preventDefault();
        selectCity(items[Math.max(citySuggest.active, 0)]);
    } else if (e.key === 'Escape') {
        if ($('cdek-city-list').hidden) return;
        e.stopPropagation();
        setCityListOpen(false);
    }
}

function selectCity(city) {
    if (!city) return;
    clearTimeout(citySuggest.timer);
    citySuggest.seq++;
    setCityListOpen(false);
    const same = delivery.city?.code === city.code;
    delivery.city = { code: city.code, name: city.name, region: city.region || '' };
    if (!same) delivery.point = null;
    $('cdek-city').value = city.name;
    $('cdek-point-filter').value = '';
    saveCheckout();
    haptic('light');
    if (!delivery.point) showPoints();
    renderDelivery();
    autoQuote();
}

// --- СДЭК: пункты выдачи ---

async function showPoints() {
    const city = delivery.city;
    if (!city) return;
    pointsView.limit = POINTS_PAGE;
    if (pointsCache.has(city.code)) {
        pointsView = { list: pointsCache.get(city.code), limit: POINTS_PAGE, loading: false, error: '' };
        renderPoints();
        return;
    }
    pointsView = { list: [], limit: POINTS_PAGE, loading: true, error: '' };
    renderPoints();
    try {
        const response = await fetch(`api/delivery/cdek/points?city=${encodeURIComponent(city.code)}`);
        const data = await response.json().catch(() => ({}));
        if (!response.ok || !data.ok) throw new Error(data.description || 'Не удалось загрузить пункты выдачи. Попробуйте ещё раз.');
        const list = Array.isArray(data.points) ? data.points : [];
        pointsCache.set(city.code, list);
        if (delivery.city?.code !== city.code) return;
        pointsView = { list, limit: POINTS_PAGE, loading: false, error: '' };
    } catch (error) {
        if (delivery.city?.code !== city.code) return;
        pointsView = { list: [], limit: POINTS_PAGE, loading: false, error: error instanceof TypeError ? 'Нет связи с сервером. Попробуйте ещё раз.' : error.message };
    }
    renderPoints();
    renderDelivery();
}

function filteredPoints() {
    const words = $('cdek-point-filter').value.toLowerCase().split(/\s+/).filter(Boolean);
    if (!words.length) return pointsView.list;
    return pointsView.list.filter((p) => {
        const text = `${p.name} ${p.address}`.toLowerCase();
        return words.every((w) => text.includes(w));
    });
}

function renderPoints() {
    const list = $('cdek-point-list');
    const count = $('cdek-point-count');
    const more = $('cdek-point-more');
    if (pointsView.loading) {
        count.innerText = 'Загружаем пункты выдачи…';
        list.innerHTML = '';
        more.hidden = true;
        return;
    }
    const all = filteredPoints();
    const shown = all.slice(0, pointsView.limit);
    if (!pointsView.list.length) count.innerText = pointsView.error ? '' : 'В этом городе нет пунктов выдачи СДЭК. Выберите соседний город.';
    else if (!all.length) count.innerText = 'Ничего не нашлось. Попробуйте другую улицу.';
    else count.innerText = all.length > shown.length ? `Пункты выдачи: ${shown.length} из ${all.length}` : `Пункты выдачи: ${all.length}`;
    list.innerHTML = shown.map((p) => `
        <li><button type="button" class="point-item" data-code="${esc(p.code)}">
            <span class="point-item-addr">${esc(p.address)}</span>
            <span class="point-item-meta">${esc(p.name)}${p.work_time ? `<br>${esc(p.work_time)}` : ''}</span>
        </button></li>`).join('');
    more.hidden = all.length <= shown.length;
}

function selectPoint(code) {
    const p = pointsView.list.find((x) => String(x.code) === String(code));
    if (!p) return;
    delivery.point = { code: p.code, name: p.name, address: p.address, work_time: p.work_time || '' };
    saveCheckout();
    haptic('light');
    renderDelivery();
    autoQuote();
    $('cdek-point-card').querySelector('.text-link')?.focus({ preventScroll: true });
}

function changePoint() {
    delivery.point = null;
    delivery.quote = null;
    saveCheckout();
    showPoints();
    renderDelivery();
    $('cdek-point-filter').focus();
}

function bindDelivery() {
    document.querySelectorAll('input[name="delivery-method"]').forEach((r) => r.addEventListener('change', onMethodChange));
    const city = $('cdek-city');
    city.addEventListener('input', onCityInput);
    city.addEventListener('keydown', onCityKey);
    city.addEventListener('focus', () => { if (!delivery.city && (citySuggest.items === null || citySuggest.items.length)) renderCityList(); });
    city.addEventListener('blur', () => setCityListOpen(false));
    const list = $('cdek-city-list');
    list.addEventListener('mousedown', (e) => e.preventDefault()); // иначе blur поля закроет список до клика
    list.addEventListener('click', (e) => {
        const li = e.target.closest('.suggest-item');
        if (li) selectCity(citySuggest.items[Number(li.dataset.idx)]);
    });
    $('cdek-point-filter').addEventListener('input', () => {
        pointsView.limit = POINTS_PAGE;
        renderPoints();
    });
    $('cdek-point-list').addEventListener('click', (e) => {
        const btn = e.target.closest('.point-item');
        if (btn) selectPoint(btn.dataset.code);
    });
    $('cdek-point-more').addEventListener('click', () => {
        pointsView.limit += POINTS_PAGE;
        renderPoints();
    });
    $('checkout-address').addEventListener('input', () => {
        quoteState.error = '';
        renderDelivery();
    });
    $('pochta-quote-btn').addEventListener('click', () => {
        haptic('light');
        requestQuote();
    });
}

// --- форма ---

// Поля формы ровно как в теле POST api/order (без корзины).
function readCheckout() {
    const data = Object.fromEntries(CHECKOUT_FIELDS.map((f) => [f, $(`checkout-${f}`).value.trim()]));
    delete data.address; // адрес — только у Почты, его кладёт deliveryPayload
    CONSENT_FIELDS.forEach((f) => { data[f] = $(`checkout-${f}`).checked; });
    return { ...data, ...deliveryPayload() };
}

// В localStorage — ещё и выбранные город/пункт целиком, чтобы показать их без запроса к СДЭК.
function saveCheckout() {
    store.set(CHECKOUT_KEY, {
        ...readCheckout(),
        address: $('checkout-address').value,
        cdek_city: delivery.city,
        cdek_point_info: delivery.point,
        pochta_quoted_for: delivery.pochtaQuotedFor,
    });
}

// Кнопка оплаты доступна, только когда стоят обе галочки и доставка рассчитана для этой корзины
function syncSubmit() {
    const btn = $('submit-order-btn');
    const ready = quoteValid();
    btn.disabled = submitting || !checkoutCfg.enabled || !ready || !CONSENT_FIELDS.every((f) => $(`checkout-${f}`).checked);
    if (!submitting) btn.innerText = ready ? `Перейти к оплате · ${rub(delivery.quote.total)}` : 'Перейти к оплате';
}

function loadCheckoutData() {
    const data = store.get(CHECKOUT_KEY, null);
    if (!data) return;
    CHECKOUT_FIELDS.forEach((f) => { if (data[f]) $(`checkout-${f}`).value = data[f]; });
    const radio = document.querySelector(`input[name="delivery-method"][value="${CSS.escape(data.delivery || '')}"]`);
    if (radio) radio.checked = true;
    delivery.method = deliveryMethod();
    const city = data.cdek_city;
    if (city && Number.isInteger(city.code) && typeof city.name === 'string') {
        delivery.city = { code: city.code, name: city.name, region: String(city.region || '') };
        $('cdek-city').value = city.name;
        const p = data.cdek_point_info;
        if (p && p.code != null && String(p.code) === String(data.cdek_point)) {
            delivery.point = { code: p.code, name: String(p.name || ''), address: String(p.address || ''), work_time: String(p.work_time || '') };
        }
    }
    delivery.pochtaQuotedFor = typeof data.pochta_quoted_for === 'string' ? data.pochta_quoted_for : '';
    // галочки переживают уход на подтверждение в Telegram: заказ после возврата отправляется сам
    CONSENT_FIELDS.forEach((f) => { $(`checkout-${f}`).checked = data[f] === true; });
}

function clearCheckout() {
    store.remove(CHECKOUT_KEY);
    CHECKOUT_FIELDS.forEach((f) => { $(`checkout-${f}`).value = ''; });
    CONSENT_FIELDS.forEach((f) => { $(`checkout-${f}`).checked = false; });
    delivery = { method: deliveryMethod(), city: null, point: null, pochtaQuotedFor: '', quote: null };
    $('cdek-city').value = '';
    $('cdek-point-filter').value = '';
    renderDelivery();
}

async function processOrder() {
    if (submitting) return;
    syncCart();
    if (!cart.length) {
        resetTo('catalog');
        showCustomAlert('Ваша корзина пуста или товары были обновлены.');
        return;
    }
    await checkoutReady;
    if (!checkoutCfg.enabled) {
        renderCheckout();
        return;
    }
    const form = readCheckout();
    const required = ['name', 'surname', 'patronymic', 'telegram', 'email'];
    if (required.some((f) => !form[f]) || form.phone.length !== 15) {
        showCustomAlert('Пожалуйста, заполните все обязательные поля!');
        return;
    }
    if (!deliveryComplete()) {
        showCustomAlert(delivery.method === 'pochta' ? 'Укажите адрес для доставки Почтой и нажмите «Рассчитать».' : 'Выберите город и пункт выдачи СДЭК.');
        return;
    }
    if (!form.consent || !form.offer) {
        showCustomAlert('Чтобы оформить заказ, отметьте согласие на обработку персональных данных и условия оферты.');
        return;
    }
    // Сайт без входа: заказ создаётся только после подтверждения через Telegram
    if (!IN_TG && !account.user) {
        if (account.loginEnabled) openTgConfirm();
        else showCustomAlert(LOGIN_RESULT.disabled);
        return;
    }

    const button = $('submit-order-btn');
    submitting = true;
    button.disabled = true;
    button.innerText = 'Создаём оплату...';
    try {
        // сумму на кнопке человек должен видеть до оплаты: без свежего расчёта не отправляем
        if (!(await ensureQuote())) return;
        const response = await fetch('api/order', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'X-TG-Init-Data': tg?.initData || '' },
            body: JSON.stringify({ ...form, cart: cartPayload() }),
        });
        const data = await response.json().catch(() => ({}));
        if (response.status === 401 && !IN_TG) {
            account.user = null; // вход истёк или выполнен выход в другой вкладке — подтверждаем заново
            renderAccountSlot();
            renderLandingUser();
            if (account.loginEnabled) openTgConfirm();
            else showCustomAlert(LOGIN_RESULT.disabled);
            return;
        }
        if (response.status === 503) retryCheckoutConfig(); // оформление выключили — покажем заглушку
        if (response.status === 400) delivery.quote = null; // сервер мог посчитать доставку иначе — пересчитаем
        if (!response.ok || !data.ok) throw new Error(data.description || `Системный сбой сервера. Код: ${response.status}`);

        hapticResult('success');
        if (data.order.notified === false && account.user) {
            account.user.needs_bot_start = true;
            renderBotAlert();
        }
        cart = [];
        saveCart();
        updateCartBadge();
        clearCheckout();
        startPayment(data.order);
    } catch (error) {
        const message = error instanceof TypeError ? 'нет связи с сервером' : error.message;
        showCustomAlert(`Заказ не оформлен: ${message}`);
    } finally {
        submitting = false;
        syncSubmit();
        if (!delivery.quote) autoQuote();
    }
}

// --- оплата ---
// Сайт уходит на страницу банка и возвращается с ?payment=…; мини-апп открывает оплату в браузере и ждёт здесь,
// спрашивая сервер раз в 3 с (сервер сам сверяется с банком). Опрос идёт, только пока экран открыт и виден.

const PAY_TEXT = {
    unknown: 'Если оплата прошла, статус обновится в разделе «Текущие заказы».',
    cancelled: 'Заказ отменён: оплата не поступила. Оформите заказ заново, если он ещё нужен.',
    expired: 'Время на оплату вышло, заказ отменён. Оформите заказ заново, если он ещё нужен.',
    refunded: 'Деньги за заказ возвращены.',
};
const PAY_POLL_MS = 3000;
const PAY_RETURN_TRIES = 40; // после возврата с «успехом» ждём подтверждения банка ~2 минуты
let pay = null; // { id, total, url, mode: 'wait' | 'success' | 'fail', status, tries, timer, busy, note }

// Ссылку на оплату даёт сервер, но в href/openLink кладём только https — на всякий случай.
const safePayUrl = (url) => (/^https:\/\//i.test(String(url || '')) ? String(url) : null);

function openPayment(url) {
    const link = safePayUrl(url);
    if (!link) return;
    haptic('light');
    if (IN_TG && tg?.openLink) tg.openLink(link);
    else location.assign(link);
}

function startPayment(order) {
    const url = safePayUrl(order.payment_url);
    if (!IN_TG) {
        if (url) {
            location.assign(url);
            return;
        }
    } else if (url) {
        tg.openLink?.(url);
    }
    pay = { id: order.id, total: order.total, url, mode: 'wait', status: 'pending', tries: 0, timer: 0, busy: false, note: '' };
    resetTo('payment'); // «назад» с экрана оплаты — в каталог, а не к повторной отправке
}

function resumePayPoll() {
    if (!pay || pay.busy || pay.timer || pay.note) return;
    if (pay.status && pay.status !== 'pending') return;
    pollPayment();
}

async function pollPayment() {
    const p = pay;
    if (!p) return;
    p.timer = 0;
    if (currentScreen() !== 'payment' || document.visibilityState === 'hidden') return; // вернётся — продолжим
    p.busy = true;
    try {
        const response = await fetch(`api/order/${encodeURIComponent(p.id)}/payment`, { headers: { 'X-TG-Init-Data': tg?.initData || '' } });
        const data = await response.json().catch(() => ({}));
        if (response.status === 401) p.note = PAY_TEXT.unknown; // открыли не там, где оформляли, — статус не узнать
        else if (response.ok && data.ok) {
            p.status = data.payment_status || null;
            p.url = safePayUrl(data.payment_url);
            if (data.total > 0) p.total = data.total; // после возврата с сайта банка сумму знаем только от сервера
        } else if (response.status < 500 && response.status !== 429) p.note = data.description || PAY_TEXT.unknown;
    } catch { /* нет сети — спросим ещё раз */ }
    p.busy = false;
    if (p !== pay) return;
    p.tries += 1;
    if (!p.note && p.status === 'paid') hapticResult('success');
    const waiting = !p.note && (p.status === 'pending' || p.status == null);
    if (waiting && p.mode === 'success' && p.tries >= PAY_RETURN_TRIES) p.note = PAY_TEXT.unknown;
    else if (waiting) p.timer = setTimeout(pollPayment, PAY_POLL_MS);
    if (currentScreen() === 'payment') renderPayment();
}

function renderPayment() {
    const box = $('payment-box');
    if (!pay) {
        box.innerHTML = '';
        return;
    }
    const p = pay;
    const st = p.status;
    let title;
    let text;
    let actions = '';
    let live = false;
    const ordersBtn = '<button type="button" class="order-btn" onclick="navigate(\'orders\')">Текущие заказы</button>';
    const catalogBtn = '<button type="button" class="main-action-btn" onclick="resetTo(\'catalog\')">В каталог</button>';
    if (st === 'paid') {
        title = 'Оплата прошла';
        text = 'Спасибо за заказ! Статус и трек-номер появятся в разделе «Текущие заказы»' + (IN_TG ? ', бот пришлёт их в личные сообщения.' : '.');
        actions = catalogBtn + ordersBtn;
    } else if (p.note) {
        title = p.mode === 'fail' ? 'Оплата не прошла' : 'Статус оплаты';
        text = p.note;
        actions = catalogBtn + ordersBtn;
    } else if (PAY_TEXT[st]) {
        title = st === 'refunded' ? 'Деньги возвращены' : 'Заказ отменён';
        text = PAY_TEXT[st];
        actions = catalogBtn + ordersBtn;
    } else if (p.mode === 'fail') {
        title = 'Оплата не прошла';
        text = p.url ? 'Деньги не списаны. Можно повторить оплату — заказ сохранён.' : 'Деньги не списаны. Статус заказа — в разделе «Текущие заказы».';
        actions = (p.url ? `<button type="button" class="main-action-btn" onclick="openPayment(pay.url)">Оплатить${p.total ? ` · ${rub(p.total)}` : ''}</button>` : '') + ordersBtn;
        live = true;
    } else if (p.mode === 'wait') {
        title = 'Ожидаем оплату';
        text = 'Страница оплаты открылась в браузере. После оплаты вернитесь сюда — статус обновится сам.';
        actions = (p.url ? '<button type="button" class="main-action-btn" onclick="openPayment(pay.url)">Открыть оплату снова</button>' : '') + ordersBtn;
        live = true;
    } else {
        title = 'Проверяем оплату';
        text = 'Банк подтверждает платёж, обычно это несколько секунд.';
        actions = ordersBtn;
        live = true;
    }
    box.innerHTML = `
        <h1 class="main-title pay-title">${esc(title)}</h1>
        <div class="pay-card">
            <div class="pay-row"><span>Заказ</span><b>#${esc(p.id)}</b></div>
            ${p.total ? `<div class="pay-row"><span>Сумма</span><b>${rub(p.total)}</b></div>` : ''}
            ${live ? '<div class="pay-progress" aria-hidden="true"></div>' : ''}
        </div>
        <p class="pay-text" role="status">${esc(text)}</p>
        <div class="pay-actions">${actions}</div>`;
}

// Возврат со страницы банка: /?payment=success&order=12. Адрес чистим, чтобы «обновить» не повторяло экран.
function handlePaymentReturn() {
    const params = new URLSearchParams(location.search);
    const result = params.get('payment');
    const id = parseInt(params.get('order'), 10);
    if (!result) return;
    history.replaceState(null, '', location.pathname);
    if (!(id > 0)) return;
    pay = { id, total: 0, url: null, mode: result === 'success' ? 'success' : 'fail', status: null, tries: 0, timer: 0, busy: false, note: '' };
    resetTo('payment');
}

// Вернулись из браузера с оплатой в мини-апп — спрашиваем сразу, не дожидаясь таймера.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || !pay) return;
    clearTimeout(pay.timer);
    pay.timer = 0;
    resumePayPoll();
});

// --- вход на сайт через Telegram ---
// В мини-аппе ничего этого не видно: там покупателя подтверждает сам Telegram (initData).

const LOGIN_RESULT = {
    cancelled: 'Подтверждение отменено, заказ не оформлен. Данные сохранены — нажмите «Перейти к оплате», когда будете готовы.',
    expired: 'Подтверждение не завершено: ссылка устарела или открылась в другом браузере. Заказ не оформлен — нажмите «Перейти к оплате» ещё раз и подтвердите в этом же браузере.',
    failed: 'Telegram не подтвердил вход, заказ не оформлен. Попробуйте ещё раз через минуту.',
    disabled: 'Оформить заказ на сайте пока нельзя. Заказы принимаются в приложении магазина в Telegram.',
};

async function loadAccount() {
    try {
        const response = await fetch('api/me', { headers: { 'X-TG-Init-Data': tg?.initData || '' } });
        const data = await response.json();
        account = { loginEnabled: Boolean(data.login_enabled), botUsername: data.bot_username || null, user: data.user || null };
    } catch {
        account = { loginEnabled: false, botUsername: null, user: null };
    }
    renderAccountSlot();
    renderLandingUser();
    renderBotAlert();
    prefillFromAccount();
}

// --- окно «Последний шаг»: подтверждение через Telegram перед созданием заказа ---
// Форма и корзина уже в localStorage. Ставим отметку «ждём подтверждения» и уходим в Telegram;
// вернувшись с login=ok, заказ отправляется сам. Не подтвердил — заказа нет.

const PENDING_KEY = 'tg_shop_pending_order';
const PENDING_TTL = 15 * 60 * 1000; // дольше — человек мог передумать, сам не отправляем

function openTgConfirm() {
    $('tg-confirm-modal').classList.add('active');
    historyStack.push('tg-confirm');
    tg?.BackButton?.show();
    haptic('medium');
}

function closeTgConfirm() {
    $('tg-confirm-modal').classList.remove('active');
    if (historyStack[historyStack.length - 1] === 'tg-confirm') historyStack.pop();
    updateBackButton();
}

function confirmViaTelegram(e) {
    saveCheckout();
    store.set(PENDING_KEY, Date.now());
    startLogin(e.currentTarget);
}

function takePendingOrder() {
    const at = store.get(PENDING_KEY, 0);
    store.remove(PENDING_KEY);
    return Date.now() - at < PENDING_TTL;
}

function startLogin(link) {
    link.dataset.label ||= link.querySelector('span').innerText;
    link.classList.add('is-busy');
    link.querySelector('span').innerText = 'Открываем Telegram...';
    haptic('light');
}

// Вернулись кнопкой «назад» со страницы Telegram — кнопка снова в исходном виде, а не «Открываем...»
window.addEventListener('pageshow', (e) => {
    if (!e.persisted) return;
    // браузер вернул страницу целиком из кэша — экран и прокрутка уже на месте, точка возврата не нужна
    try { sessionStorage.removeItem(RETURN_KEY); } catch { /* приватный режим */ }
    store.remove(PENDING_KEY);
    document.querySelectorAll('.login-btn.is-busy').forEach((link) => {
        link.classList.remove('is-busy');
        link.querySelector('span').innerText = link.dataset.label || 'Подтвердить через Telegram';
    });
});

function renderAccountSlot() {
    const slot = $('account-slot');
    const u = account.user;
    if (IN_TG || !u) {
        slot.innerHTML = '';
        return;
    }
    const who = u.username ? `@${u.username}` : (u.full_name || 'Telegram');
    slot.innerHTML = `<span class="account-name">${esc(who)}</span><button type="button" class="text-link" onclick="logout()">Выйти</button>`;
}

async function logout() {
    try {
        await fetch('api/logout', { method: 'POST' });
    } catch { /* сессия всё равно истечёт; на экране — как после выхода */ }
    account.user = null;
    renderAccountSlot();
    renderLandingUser();
    renderBotAlert();
    renderScreenInternal(currentScreen());
}

// Телефон, подтверждённый Telegram, и ник — в пустые поля формы (то, что ввёл покупатель, не трогаем).
function prefillFromAccount() {
    const u = account.user;
    const phone = $('checkout-phone');
    if (u?.phone && !phone.value) {
        phone.value = u.phone;
        saveCheckout();
    }
    const syncHint = () => { $('phone-verified').hidden = !u?.phone || phone.value !== u.phone; };
    syncHint();
    phone.oninput = syncHint;
    const telegram = $('checkout-telegram');
    if (u?.username && !telegram.value) telegram.value = `@${u.username}`;
}

// Ник на главном экране — только на сайте: в мини-аппе человек и так в своём Telegram.
function renderLandingUser() {
    const el = $('landing-user');
    const u = account.user;
    el.hidden = IN_TG || !u;
    if (el.hidden) return;
    const who = u.username ? `@${u.username}` : (u.full_name || 'Профиль');
    el.innerText = who;
    el.setAttribute('aria-label', `${who} — текущие заказы`);
}

// --- «!» в корзине: бот не может написать первым ---
// Сервер знает это по последней попытке написать (заказ, смена статуса) и по Start/блокировке бота.

const botUrl = () => `https://t.me/${account.botUsername || 'morelufs_bot'}`;

function renderBotAlert() {
    const on = Boolean(account.user?.needs_bot_start);
    $('bot-alert').classList.toggle('on', on);
    if (!on) closeBotAlert();
    const name = `@${account.botUsername || 'morelufs_bot'}`;
    $('bot-alert').querySelector('.bot-alert-name').innerText = name;
    $('bot-alert-link').href = botUrl();
}

function toggleBotAlert(e) {
    e.stopPropagation();
    const box = $('bot-alert');
    const open = !box.classList.contains('open');
    box.classList.toggle('open', open);
    $('bot-alert-btn').setAttribute('aria-expanded', String(open));
    haptic('light');
}

function closeBotAlert() {
    $('bot-alert').classList.remove('open');
    $('bot-alert-btn').setAttribute('aria-expanded', 'false');
}

// В мини-аппе чат с ботом открывается внутри Telegram, на сайте — в новой вкладке (t.me).
function openBotChat(e) {
    if (tg?.openTelegramLink && IN_TG) {
        e.preventDefault();
        tg.openTelegramLink(botUrl());
    }
}

// тап мимо плашки и Esc закрывают её
document.addEventListener('click', (e) => {
    if (!$('bot-alert').contains(e.target)) closeBotAlert();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeBotAlert();
});

// Нажал Start у бота и вернулся на сайт — перепроверяем, чтобы «!» исчез сам.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && account.user?.needs_bot_start) accountReady = loadAccount();
});

// Возврат из Telegram: /?login=ok&next=checkout. Адрес чистим, чтобы «обновить» не повторяло сообщение.
function handleLoginReturn() {
    const params = new URLSearchParams(location.search);
    const result = params.get('login');
    if (!result) return;
    history.replaceState(null, '', location.pathname);
    const next = ['orders', 'checkout'].includes(params.get('next')) ? params.get('next') : null;
    const pending = takePendingOrder();
    if (next) {
        historyStack = ['landing', 'cart', next]; // «назад» — в корзину, где начинали
        renderScreenInternal(next);
    }
    if (result !== 'ok') {
        showCustomAlert(LOGIN_RESULT[result] || LOGIN_RESULT.failed);
        return;
    }
    hapticResult('success');
    // подтвердил в Telegram — заказ, ради которого входили, отправляем сам (когда загрузится каталог и вход)
    if (pending && next === 'checkout') Promise.all([accountReady, catalogReady]).then(() => processOrder());
}

// --- текущие заказы ---

function ordersEmptyHTML() {
    const guest = !IN_TG && !account.user;
    const hint = guest && account.loginEnabled
        ? `<p class="orders-empty-hint">Заказывали раньше? <a class="text-link" href="auth/login?next=orders">Войдите через Telegram</a>, чтобы увидеть их.</p>`
          + '<p class="legal-note">Входя, вы даёте <a class="legal-link" href="consent" target="_blank" rel="noopener" data-legal>согласие на обработку персональных данных</a>.</p>'
        : '';
    return `<div class="orders-empty"><div class="empty-state"><p class="orders-empty-title">Нет текущих заказов</p>${hint ? `<div class="empty-sub">${hint}</div>` : ''}</div></div>`;
}

const PAY_STATUS = {
    pending: 'ожидает оплаты',
    paid: 'оплачен',
    cancelled: 'не оплачен, заказ отменён',
    expired: 'не оплачен вовремя, заказ отменён',
    refunded: 'деньги возвращены',
};

// Эти статусы ставит сам сервер по оплате — строка «Оплата: …» под ними повторяла бы их же
const PAY_STATUS_SHOWN = ['Ожидает оплаты', 'Оплачен', 'Отменён: не оплачен', 'Отменён, деньги возвращены'];

function orderHTML(o) {
    const items = o.items
        .map((i, n) => `${n + 1}. ${esc(i.title)} (Разм: ${esc(i.size)}) — ${i.qty} шт. х ${i.price} руб. (${i.qty * i.price} руб.)`)
        .join('\n');
    const waiting = ['Ожидает подтверждения', 'Ожидает оплаты'].includes(o.status);
    const head = `
            <div class="order-head">
                <h4>Заказ #${esc(o.id)}</h4>
                <span class="order-status${waiting ? '' : ' order-status-active'}">${esc(o.status)}</span>
            </div>
            ${o.track ? `<div class="order-track">Трек-номер: ${esc(o.track)}</div>` : ''}`;
    // заказы до онлайн-оплаты — как раньше, со способом оплаты
    if (o.payment_status == null) {
        return `
        <div class="cart-item order-card">${head}
            <div class="order-details"><b>Телефон:</b> +7 ${esc(o.phone)}
<b>Telegram:</b> ${esc(o.telegram)}
<b>Email:</b> ${esc(o.email)}
<b>Способ доставки:</b> ${esc(o.delivery)}
<b>Адрес ПВЗ:</b> ${esc(o.address)}
${o.payment ? `<b>Способ оплаты:</b> ${esc(o.payment)}\n` : ''}
<b>Состав заказа:</b>
${items}

<b>ИТОГО: ${rub(o.total)}.</b></div>
        </div>`;
    }
    const url = o.payment_status === 'pending' ? safePayUrl(o.payment_url) : null;
    const addrLabel = /почт/i.test(o.delivery || '') ? 'Адрес' : 'Пункт выдачи';
    const goods = o.goods_total ?? o.total - (o.delivery_cost || 0);
    const payBtn = !url ? '' : IN_TG
        ? `<button type="button" class="main-action-btn order-pay-btn" data-pay-url="${esc(url)}">Оплатить · ${rub(o.total)}</button>`
        : `<a class="main-action-btn order-pay-btn" href="${esc(url)}">Оплатить · ${rub(o.total)}</a>`;
    return `
        <div class="cart-item order-card">${head}
            ${PAY_STATUS_SHOWN.includes(o.status) ? '' : `<div class="order-pay order-pay-${esc(o.payment_status)}">Оплата: ${esc(PAY_STATUS[o.payment_status] || o.payment_status)}</div>`}
            <div class="order-details"><b>Телефон:</b> +7 ${esc(o.phone)}
<b>Telegram:</b> ${esc(o.telegram)}
<b>Email:</b> ${esc(o.email)}
<b>Доставка:</b> ${esc(o.delivery)}
<b>${addrLabel}:</b> ${esc(o.address)}

<b>Состав заказа:</b>
${items}</div>
            <dl class="order-sum">
                <div class="total-row"><dt>Товары</dt><dd>${rub(goods)}</dd></div>
                <div class="total-row"><dt>Доставка</dt><dd>${rub(o.delivery_cost || 0)}</dd></div>
                <div class="total-row total-row-sum"><dt>Итого</dt><dd>${rub(o.total)}</dd></div>
            </dl>
            ${payBtn}
        </div>`;
}

// «Оплатить» в мини-аппе — во внешнем браузере через Telegram, иначе страница банка заменила бы приложение
document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-pay-url]');
    if (btn) openPayment(btn.dataset.payUrl);
});

async function fetchAndRenderOrders() {
    const container = $('orders-container');
    $('orders-screen').classList.remove('has-orders');
    container.innerHTML = '<div class="loading-msg">Загрузка данных...</div>';
    await accountReady;
    renderAccountSlot();
    if (!IN_TG && !account.user) {
        container.innerHTML = ordersEmptyHTML();
        return;
    }
    try {
        const response = await fetch('api/orders', { headers: { 'X-TG-Init-Data': tg?.initData || '' } });
        if (response.status === 401 && !IN_TG) {
            account.user = null;
            renderAccountSlot();
            container.innerHTML = ordersEmptyHTML();
            return;
        }
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.description || `HTTP ${response.status}`);
        container.innerHTML = data.orders.length ? data.orders.map(orderHTML).join('') : ordersEmptyHTML();
        $('orders-screen').classList.toggle('has-orders', data.orders.length > 0);
    } catch (error) {
        const message = error instanceof TypeError ? 'Сетевая ошибка при загрузке.' : error.message;
        container.innerHTML = `<div class="loading-msg error-msg">${esc(message)}</div>`;
    }
}

// --- FAQ ---

// одновременно открыт один ответ; inert убирает ссылки закрытых ответов из Tab
function bindFaq() {
    const items = [...document.querySelectorAll('.faq-item')];
    const setOpen = (item, open) => {
        item.classList.toggle('open', open);
        item.querySelector('.faq-q').setAttribute('aria-expanded', String(open));
        item.querySelector('.faq-a').inert = !open;
    };
    items.forEach((item) => {
        item.querySelector('.faq-q').addEventListener('click', () => {
            const open = !item.classList.contains('open');
            items.forEach((other) => setOpen(other, other === item && open));
            haptic('light');
        });
    });
}

// Мини-апп: обычная ссылка в новую вкладку там ничего не открывает. t.me — внутри Telegram,
// остальные внешние (Instagram, TikTok…) — в браузере через Telegram. Ссылки со своим обработчиком
// (уже сделали preventDefault, как «Открыть бота») и документы (data-legal, ниже) не трогаем.
document.addEventListener('click', (e) => {
    if (!IN_TG || e.defaultPrevented) return;
    const a = e.target.closest('a[target="_blank"]:not([data-legal])');
    if (!a || !/^https?:/.test(a.href)) return;
    if (/^https:\/\/t\.me\//.test(a.href) && tg?.openTelegramLink) {
        e.preventDefault();
        tg.openTelegramLink(a.href);
    } else if (tg?.openLink) {
        e.preventDefault();
        tg.openLink(a.href);
    }
});

// --- документы (оферта, политика, согласие, реквизиты) ---
// Ссылки с data-legal в мини-аппе открываются во внешнем браузере через Telegram, иначе страница документа
// заменила бы приложение. На сайте — в этой же вкладке: перед уходом запоминаем, где был покупатель,
// а крестик на странице документа делает «назад» — и restoreReturnPoint возвращает тот же экран и прокрутку.
// Форма оформления не теряется: она и так сохраняется в localStorage на каждый ввод.
const RETURN_KEY = 'mls_return'; // это же имя проверяет страница документа (app/legal.py)
const RETURN_TTL = 60 * 60 * 1000;

document.addEventListener('click', (e) => {
    const link = e.target.closest('a[data-legal]');
    if (!link) return;
    if (IN_TG && tg?.openLink) {
        e.preventDefault();
        e.stopPropagation(); // ссылка внутри подписи галочки — не переключать саму галочку
        tg.openLink(new URL(link.getAttribute('href'), location.href).href);
        return;
    }
    if (e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // сам попросил новую вкладку
    saveReturnPoint();
    if (link.target === '_blank') {
        e.preventDefault();
        e.stopPropagation();
        location.assign(link.href);
    }
});

function saveReturnPoint() {
    const screen = currentScreen();
    const faq = [...document.querySelectorAll('.faq-item')].findIndex((item) => item.classList.contains('open'));
    try {
        sessionStorage.setItem(RETURN_KEY, JSON.stringify({
            t: Date.now(),
            // окна поверх экрана не восстанавливаем, кроме «Последнего шага» — ссылка на согласие стоит в нём
            stack: historyStack.filter((s) => !OVERLAYS.has(s) || s === 'tg-confirm'),
            scroll: $(`${screen}-screen`)?.scrollTop || 0,
            product: currentProduct?.id ?? null,
            size: selectedSize,
            slide: currentSlideIdx,
            faq,
        }));
    } catch { /* приватный режим — вернёмся на главную */ }
}

function takeReturnPoint() {
    try {
        const saved = JSON.parse(sessionStorage.getItem(RETURN_KEY));
        sessionStorage.removeItem(RETURN_KEY);
        return saved;
    } catch {
        return null;
    }
}

// Пока догружаются каталог, заказы, пункты выдачи, высота экрана растёт — держим прокрутку на месте,
// пока покупатель сам не начнёт листать.
function holdScroll(el, y) {
    if (!el || !(y > 0)) return;
    const until = performance.now() + 2500;
    const events = ['wheel', 'touchstart', 'pointerdown', 'keydown'];
    let stopped = false;
    const stop = () => { stopped = true; };
    events.forEach((t) => document.addEventListener(t, stop, { once: true, passive: true, capture: true }));
    const tick = () => {
        if (stopped || performance.now() > until) {
            events.forEach((t) => document.removeEventListener(t, stop, { capture: true }));
            return;
        }
        if (Math.abs(el.scrollTop - y) > 1) el.scrollTop = y;
        requestAnimationFrame(tick);
    };
    tick();
}

// Вернулись со страницы документа кнопкой «назад» (крестик там — это history.back()).
// Обычный заход на сайт или переход по логотипу — не «назад», там стартовый экран.
async function restoreReturnPoint() {
    const saved = takeReturnPoint();
    const nav = performance.getEntriesByType?.('navigation')?.[0];
    if (!saved || nav?.type !== 'back_forward' || !(Date.now() - saved.t < RETURN_TTL)) return;
    const stack = (Array.isArray(saved.stack) ? saved.stack : [])
        .filter((s) => typeof s === 'string' && (s === 'tg-confirm' || (SECTION[s] && $(`${s}-screen`))));
    if (stack.length < 2 || stack[0] !== 'landing' || historyStack.length > 1) return; // уже ушли с главной сами
    await catalogReady;
    if (historyStack.length > 1) return;
    if (stack.includes('product')) {
        if (!fillProduct(saved.product, saved.size)) return; // товар убрали из каталога
        const n = currentProduct.images.length;
        if (saved.slide > 0 && saved.slide < n) setSlide(saved.slide);
    }
    const overlay = stack[stack.length - 1] === 'tg-confirm';
    historyStack = overlay ? stack.slice(0, -1) : stack;
    const screen = currentScreen();
    if (screen === 'checkout') await checkoutReady;
    renderScreenInternal(screen);
    const faq = document.querySelectorAll('.faq-item')[saved.faq];
    if (screen === 'catalog' && faq && !faq.classList.contains('open')) faq.querySelector('.faq-q').click();
    if (overlay) openTgConfirm();
    holdScroll($(`${screen}-screen`), saved.scroll);
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
    // В мини-аппе свайпы вверх/вниз не сворачивают и не закрывают приложение: в каталоге и карточке
    // почти весь экран занимают фото, карусели и видео, и жест легко задеть при листании.
    // Свернуть/закрыть по-прежнему можно кнопкой в шапке Telegram — она работает всегда.
    if (IN_TG) tg?.disableVerticalSwipes?.();
    // В мини-аппе есть системная кнопка «назад», поэтому части шапки там можно прятать.
    document.body.classList.toggle('in-tg', Boolean(tg?.initData));
    const phone = $('checkout-phone');
    phone.addEventListener('input', (e) => { e.target.value = formatPhone(e.target.value); });

    loadCheckoutData();
    bindDelivery();
    document.querySelectorAll('#checkout-screen input, #checkout-screen textarea').forEach((el) => {
        el.addEventListener('input', saveCheckout);
        el.addEventListener('change', saveCheckout);
    });
    document.querySelectorAll('.radio-input').forEach((r) => r.addEventListener('change', () => haptic('light')));
    CONSENT_FIELDS.forEach((f) => $(`checkout-${f}`).addEventListener('change', () => { haptic('light'); syncSubmit(); }));
    renderDelivery();

    bindFaq();
    bindSwipe($('main-carousel'));
    bindLightboxGestures($('lightbox-carousel'));
    $('product-screen')?.addEventListener('scroll', syncTopbar, { passive: true });
    $('brand-screen')?.addEventListener('scroll', syncTopbar, { passive: true });
    // клик по тёмному полю вокруг фото закрывает просмотр (клики по самому фото — в жестах)
    $('lightbox-modal').addEventListener('click', (e) => {
        if (e.target === e.currentTarget || e.target.id === 'lightbox-counter') closeLightbox();
    });

    updateCartBadge();
    preloadCollectionPhotos();
    accountReady = loadAccount();
    catalogReady = loadCatalog();
    checkoutReady = loadCheckoutConfig();
    renderScreenInternal('landing');
    handleLoginReturn();
    handlePaymentReturn();
    restoreReturnPoint();
}

init();
