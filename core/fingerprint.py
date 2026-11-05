# -*- coding: utf-8 -*-
"""
fingerprint.py - отпечаток браузера, свой у каждого ящика.

Задача не «стать невидимкой», а не быть однотипным: два разных ящика на одном
компьютере не должны выглядеть как один и тот же браузер. Поэтому отпечаток
считается ДЕТЕРМИНИРОВАННО от id ящика - он у ящика один и тот же навсегда.
Менять его на каждом запуске ХУЖЕ, чем не менять вовсе: сайт видел бы
«прыгающий» браузер, а это само по себе подозрительно. Стабильность + правдоподобие
дают больше, чем максимум случайности.

Что варьируем (всё правдоподобно для Windows + Chrome):
  размер экрана и окна (не больше реального монитора - иначе край окна уедет
  за экран и человек не увидит капчу), число ядер, объём памяти, вендор/модель
  видеокарты в WebGL, лёгкий детерминированный шум canvas (свой у каждого ящика).

Что СОЗНАТЕЛЬНО не трогаем и почему:
  • User-Agent и Sec-CH-UA - оставляем настоящие (реальную версию движка):
    подменённый UA противоречит заголовкам Client Hints, и такая нестыковка
    палится сильнее, чем помогает;
  • язык и часовой пояс - системные: они честно совпадают с настоящим адресом,
    а выдуманный пояс под чужим IP - классическая нестыковка.

Всё это бесплатно и один раз: отпечаток применяется на старте браузера
(параметры контекста + один init-скрипт) и живёт сам.
"""
import hashlib
import json
import random

# Экран - реальные популярные разрешения ноутбуков/мониторов.
_SCREENS = [(1366, 768), (1440, 900), (1536, 864), (1600, 900),
            (1920, 1080), (1920, 1200), (2560, 1440)]

_CORES = [4, 6, 8, 12, 16]
_MEM = [4, 8, 16]

# (webgl vendor, webgl renderer, короткое имя для журнала)
_GPUS = [
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "Intel UHD 620"),
    ("Google Inc. (Intel)",
     "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "Intel Iris Xe"),
    ("Google Inc. (NVIDIA)",
     "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "NVIDIA GTX 1650"),
    ("Google Inc. (AMD)",
     "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
     "AMD RX 580"),
]

# Языки - системные для русской Windows с английским вторым.
_LANGS = ["ru-RU", "ru", "en-US", "en"]

# Window size chrome (рамка окна: вкладки + адресная строка).
_CHROME_H = [90, 105, 120, 136]

# Потолок размера окна: лучше меньше, чем больше. Большое окно (1920x1080)
# Windows при масштабе 125% всё равно обрежет, а страница окажется ШИРЕ окна
# (12.09.2026: страница 1920x944 при реальном окне 1554x882 - часть не видно).
_MAX_W, _MAX_H = 1440, 900


def for_mailbox(mailbox_id, langs=None, max_screen=None):
    """Отпечаток для ящика. Один и тот же для одного id - всегда.

    max_screen - РЕАЛЬНЫЙ размер монитора машины. Окно не должно быть больше
    экрана: иначе его край уезжает за пределы видимой области и человек не
    увидит капчу. Поэтому выбираем только из размеров, которые влезают."""
    seed = int(hashlib.md5(("mb%s" % mailbox_id).encode("utf-8")).hexdigest()[:12], 16)
    rnd = random.Random(seed)
    screens = [s for s in _SCREENS if s[0] <= _MAX_W and s[1] <= _MAX_H] or _SCREENS
    if max_screen and max_screen[0] and max_screen[1]:
        fit = [s for s in screens if s[0] <= max_screen[0] and s[1] <= max_screen[1]]
        if fit:
            screens = fit
        else:
            screens = [(int(max_screen[0]) // 2 * 2, int(max_screen[1]) // 2 * 2)]
    w, h = rnd.choice(screens)
    chrome_h = rnd.choice(_CHROME_H)
    view_h = max(500, h - chrome_h)
    vendor, renderer, gpu_name = rnd.choice(_GPUS)
    cores = rnd.choice(_CORES)
    mem = rnd.choice(_MEM)
    fp = {
        "mailbox_id": mailbox_id,
        "screen": {"width": w, "height": h},
        "viewport": {"width": w, "height": view_h},
        "window": (w, h),
        "chrome_h": chrome_h,
        "cores": cores,
        "mem": mem,
        "gl_vendor": vendor,
        "gl_renderer": renderer,
        "langs": list(langs or _LANGS),
        "seed": seed % 2147483647 or 7,
    }
    fp["label"] = "%dx%d, %d ядер, %d ГБ, %s" % (w, h, cores, mem, gpu_name)
    return fp


def init_script(fp):
    """JS-код, который выполняется в каждой странице до её скриптов.
    Значения подставляем как JSON - без «\n» внутри JS-строк (это дважды
    ломало драйвер регистрации: Python превращал их в настоящий перевод строки)."""
    cfg = json.dumps({
        "cores": fp["cores"],
        "mem": fp["mem"],
        "langs": fp["langs"],
        "gl_vendor": fp["gl_vendor"],
        "gl_renderer": fp["gl_renderer"],
        "seed": fp["seed"],
    }, ensure_ascii=False)
    return _JS.replace("__FP__", cfg)


_JS = """
(() => {
  const fp = __FP__;
  const def = (obj, prop, get) => {
    try { Object.defineProperty(obj, prop, {get: get, configurable: true}); }
    catch (e) {}
  };
  // след автоматизации
  def(navigator, 'webdriver', () => undefined);
  if (navigator.webdriver === true) { try { delete navigator.webdriver; } catch (e) {} }
  // «железо» и языки
  def(navigator, 'hardwareConcurrency', () => fp.cores);
  def(navigator, 'deviceMemory', () => fp.mem);
  def(navigator, 'language', () => fp.langs[0]);
  def(navigator, 'languages', () => fp.langs.slice());
  // видеокарта (её видно только через WebGL)
  try {
    const patchParam = (proto) => {
      if (!proto) return;
      const orig = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) return fp.gl_vendor;
        if (p === 37446) return fp.gl_renderer;
        return orig.apply(this, arguments);
      };
    };
    patchParam(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patchParam(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch (e) {}
  // лёгкий детерминированный шум canvas: ±1 в редких пикселях,
  // глазу не видно, а «холст» у каждого ящика свой
  try {
    const noise = (data) => {
      let s = fp.seed;
      for (let i = 0; i < data.length; i += 397) {
        s = (s * 1103515245 + 12345) & 0x7fffffff;
        data[i] = (data[i] + (s % 3) - 1) & 255;
      }
    };
    const origGet = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function () {
      const r = origGet.apply(this, arguments);
      try { noise(r.data); } catch (e) {}
      return r;
    };
    const origTo = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function () {
      try {
        const ctx = this.getContext('2d');
        if (ctx && this.width && this.height) {
          const d = ctx.getImageData(0, 0, this.width, this.height);
          noise(d.data);
          ctx.putImageData(d, 0, 0);
        }
      } catch (e) {}
      return origTo.apply(this, arguments);
    };
  } catch (e) {}
})();
"""
