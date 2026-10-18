/* ==========================================================================
   实时取景拍照（公共模块）—— 诊断页的「调用摄像头拍摄」用这一份。

   为什么要有这个文件：
     原先那个 `<input type="file" accept="image/*" capture="environment">` 在桌面
     浏览器与多数 WebView 里会**直接忽略 capture 属性**，点下去弹出来的是「选择
     文件」而不是摄像头 —— 用户报的「点击后是选择照片」就是这个。要真的开摄像头，
     只有 getUserMedia 一条路。

   它为什么不能并进 diagnose.js：
     单元测试明确断言 diagnose.js 与 library.js 里**不许出现 getUserMedia**：
     「采集能力原语必须只有一份」，采集链里的坑（录音那边是 gain=0 的静音节点）
     复制第二遍必然分叉。所以这里和 recorder.js 一样单独成文件、挂在 window.Camera。

   它只负责**取景 → 快门 → 交出一个 File**，不碰诊断页的 DOM、不碰 UI：
     产出的 File 与 <input type="file"> 交出来的完全一样，直接喂给既有的
     setImage(file) 即可 —— 类型/体积校验、缩略图预览、state.imageFile、
     FormData.append("image", …) → POST /api/diagnose/multimodal 全都不用改。

   安全上下文（重要）：getUserMedia 只在 https 或 localhost/127.0.0.1 下存在。
     手机上用 http://<局域网IP>:8000 打开时 navigator.mediaDevices 就是 undefined，
     supported() 会返回 false —— 这不是代码坏了，是访问方式的问题。
     想让手机也能开摄像头，用 scripts/33_make_dev_cert.py + scripts/20_run_backend.py
     --https 起自签 https。
   ========================================================================== */

(function () {
  "use strict";

  const MAX_EDGE = 1920;      // 出片长边上限：手机原图动辄 4000px，压到这个尺寸对诊断绰绰有余

  /* 当前这次取景的全部句柄；null 表示没在取景。 */
  let session = null;

  /* ---- 失败原因（交给调用方决定怎么提示用户） ---- */
  const REASON = {
    UNSUPPORTED: "unsupported",   // 非安全上下文 / 浏览器不支持
    DENIED: "denied",             // 用户拒绝授权
    NO_DEVICE: "no_device",       // 没有摄像头
    ERROR: "error",               // 其它异常
  };

  function supported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  function stopStream(stream) {
    if (!stream || !stream.getTracks) return;
    stream.getTracks().forEach((t) => {
      try { t.stop(); } catch (e) { /* 单条轨道停不掉不影响别的 */ }
    });
  }

  /** DOMException.name → 我们的原因码。用户点了「拒绝」和「设备压根没有」要分开说。 */
  function reasonFrom(err) {
    const name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError") return REASON.DENIED;
    if (name === "NotFoundError" || name === "OverconstrainedError") return REASON.NO_DEVICE;
    if (name === "NotSupportedError") return REASON.UNSUPPORTED;
    return REASON.ERROR;
  }

  function buildOverlay() {
    const el = document.createElement("div");
    el.className = "modal camera-modal";
    el.setAttribute("role", "dialog");
    el.setAttribute("aria-modal", "true");
    el.setAttribute("aria-label", "摄像头取景");
    el.innerHTML =
      `<div class="modal__backdrop" data-camera-close></div>` +
      `<div class="modal__panel camera-modal__panel">` +
      `<button type="button" class="modal__close" data-camera-close aria-label="关闭摄像头">` +
      (window.Icon ? Icon.render("close", 20) : "×") +
      `</button>` +
      `<div class="camera-modal__stage">` +
      // autoplay + muted + playsinline：iOS 上缺任何一个，画面都不出来
      `<video class="camera-modal__video" autoplay playsinline muted` +
      ` aria-label="摄像头实时画面"></video>` +
      `</div>` +
      `<div class="camera-modal__status" data-camera-status>` +
      `<span class="spinner" role="status" aria-label="正在打开摄像头"></span>` +
      `<span>正在打开摄像头…</span>` +
      `</div>` +
      `<div class="camera-modal__bar">` +
      `<button type="button" class="btn btn--outline" data-camera-flip hidden>` +
      (window.Icon ? Icon.render("refresh", 18) : "") +
      `<span>切换摄像头</span></button>` +
      // 触控目标 ≥44px 由 .btn 自己的 min-height 保证
      `<button type="button" class="btn btn--primary btn--lg" data-camera-shot disabled>` +
      (window.Icon ? Icon.render("camera", 18) : "") +
      `<span>拍摄</span></button>` +
      `</div>`;
    return el;
  }

  function setStatus(text) {
    const host = session && session.overlay.querySelector("[data-camera-status]");
    if (!host) return;
    if (!text) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    host.innerHTML = `<span class="spinner" role="status" aria-label="${text}"></span><span>${text}</span>`;
  }

  /** 摄像头打开后：显示画面、启用快门、按设备数量决定要不要给「切换」按钮。 */
  function attach(stream) {
    if (!session) { stopStream(stream); return; }   // 打开过程中用户已经关掉了
    stopStream(session.stream);                     // 切换朝向时把上一条流先关掉
    session.stream = stream;

    const video = session.video;
    video.srcObject = stream;
    const played = video.play();
    if (played && played.catch) played.catch(() => { /* 自动播放被拦：用户点快门时仍会出画面 */ });

    setStatus("");
    const shot = session.overlay.querySelector("[data-camera-shot]");
    if (shot) shot.disabled = false;

    if (navigator.mediaDevices.enumerateDevices) {
      navigator.mediaDevices.enumerateDevices()
        .then((list) => {
          const cams = list.filter((d) => d.kind === "videoinput").length;
          const flip = session && session.overlay.querySelector("[data-camera-flip]");
          if (flip) flip.hidden = cams < 2;
        })
        .catch(() => { /* 数不出来就不给切换按钮 */ });
    }
  }

  function startStream(facing) {
    if (!session) return;
    setStatus("正在打开摄像头…");
    const shot = session.overlay.querySelector("[data-camera-shot]");
    if (shot) shot.disabled = true;

    const want = facing === "user" ? "user" : "environment";
    const first =
      want === "user"
        ? { facingMode: "user", width: { ideal: MAX_EDGE } }
        : { facingMode: { ideal: "environment" }, width: { ideal: MAX_EDGE } };

    navigator.mediaDevices.getUserMedia({ video: first, audio: false })
      .then(attach)
      .catch(() => {
        // 有些设备只给一个摄像头、或根本不认 facingMode —— 退一步不指定朝向再来一次
        if (!session) return;
        return navigator.mediaDevices.getUserMedia({ video: true, audio: false })
          .then(attach)
          .catch((err) => {
            const onFail = session && session.onFail;
            close();
            if (onFail) onFail(reasonFrom(err));
          });
      });
  }

  /** 快门：把当前画面画到 canvas，导出 JPEG，包成 File 交给调用方。 */
  function shoot() {
    if (!session || !session.stream) return;
    const video = session.video;
    const w = video.videoWidth;
    const h = video.videoHeight;
    if (!w || !h) return;                           // 还没出画面，按了也白按

    const scale = Math.min(1, MAX_EDGE / Math.max(w, h));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);

    const ctx = canvas.getContext("2d");
    // 前置摄像头在屏幕上通常是镜像显示的，出片要翻回来，否则照片里的字是反的
    if (session.facing === "user") {
      ctx.translate(canvas.width, 0);
      ctx.scale(-1, 1);
    }
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

    const onShot = session.onShot;
    const onFail = session.onFail;
    canvas.toBlob(
      (blob) => {
        if (!blob) {
          if (onFail) onFail(REASON.ERROR);
          return;
        }
        const file = new File([blob], "camera-" + Date.now() + ".jpg", { type: "image/jpeg" });
        close();
        if (onShot) onShot(file);
      },
      "image/jpeg",
      0.92
    );
  }

  /** 收摊：**必须**把轨道停掉，否则手机上摄像头指示灯会一直亮着。 */
  function close() {
    const s = session;
    session = null;
    if (!s) return;
    stopStream(s.stream);
    if (s.video) {
      try { s.video.srcObject = null; } catch (e) { /* 老浏览器可能只读 */ }
    }
    document.removeEventListener("keydown", onKeydown);
    document.body.classList.remove("modal-open");
    if (s.overlay && s.overlay.parentNode) s.overlay.parentNode.removeChild(s.overlay);
  }

  function onKeydown(ev) {
    if (ev.key === "Escape" || ev.key === "Esc") close();
  }

  /* 切到后台就收摊：手机上一旦切走，摄像头指示灯常亮既费电又吓人。 */
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && session) close();
  });

  /**
   * 打开取景层。
   * @param {(file: File) => void} onShot  拍到照片时的回调
   * @param {(reason: string) => void} [onFail]  打不开时的回调，reason 见 REASON
   * @returns {boolean} 是否真的开始取景（false 表示当前环境不支持，调用方直接回落）
   */
  function open(onShot, onFail) {
    if (session) return true;
    if (!supported()) {
      if (onFail) onFail(REASON.UNSUPPORTED);
      return false;
    }

    const overlay = buildOverlay();
    document.body.appendChild(overlay);
    document.body.classList.add("modal-open");

    const video = overlay.querySelector("video");
    session = {
      overlay: overlay,
      video: video,
      stream: null,
      facing: "environment",
      onShot: onShot,
      onFail: onFail,
    };

    overlay.querySelectorAll("[data-camera-close]").forEach((el) => {
      el.addEventListener("click", close);
    });
    const shot = overlay.querySelector("[data-camera-shot]");
    if (shot) shot.addEventListener("click", shoot);
    const flip = overlay.querySelector("[data-camera-flip]");
    if (flip) {
      flip.addEventListener("click", () => {
        if (!session) return;
        session.facing = session.facing === "user" ? "environment" : "user";
        startStream(session.facing);
      });
    }
    document.addEventListener("keydown", onKeydown);

    startStream("environment");
    return true;
  }

  window.Camera = { supported: supported, open: open, close: close, REASON: REASON };
})();
