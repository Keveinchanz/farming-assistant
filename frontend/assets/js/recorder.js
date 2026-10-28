/* ==========================================================================
   录音采集（公共模块）—— 诊断页与知识图书库的语音检索共用这一份。

   为什么要有这个文件：这段逻辑原先私有在 diagnose.js 里，绑定诊断页的 DOM
   （btn-record / record-status / audio-preview…）。知识图书库的搜索框也要录音时，
   复制一份等于把下面这两个坑一起复制一遍。它只负责**采集与封装**，
   不碰 DOM、不碰 UI/Api —— 界面怎么呈现由各页面自己决定。

   坑一：不用 MediaRecorder。
       浏览器 MediaRecorder 默认产出 webm/opus，而语音接口要的是 16kHz 单声道 PCM。
       所以这里改用 Web Audio API 采集原始 PCM，线性重采样到 16kHz 单声道后
       自行封装成 WAV 再上传 —— 不做这一步，后端必然报「不支持的音频格式」。

   上传已有音频文件走下面的 fileToWav()：用的是浏览器自带的解码器
   （decodeAudioData），mp3 / m4a(aac) / wav / ogg / opus / flac / webm / aiff
   都能解，解完同样重采样到 16kHz 单声道 WAV。这样「支持哪些格式」由浏览器决定，
   后端不必为此装 ffmpeg。

   坑二：采集链必须经一个增益为 0 的节点再连到输出。
       直接 processor.connect(ctx.destination) 的话，麦克风采集到的声音会从扬声器
       实时放出来，形成啸叫/回声，用户会以为程序坏了。见下面 gain.value = 0 那段。
   ========================================================================== */

(function () {
  "use strict";

  const TARGET_SAMPLE_RATE = 16000;   // 识别接口只接受 16000（或 8000）采样率

  /* 当前这次采集的全部句柄；null 表示没在录。 */
  let rec = null;
  let timerId = null;

  const listeners = { start: [], tick: [], stop: [] };

  function emit(type, payload) {
    // 复制一份再遍历：监听器里调用 on()/取消订阅时不该打乱正在进行的派发
    listeners[type].slice().forEach((cb) => {
      try {
        cb(payload);
      } catch (e) {
        /* 一个监听器抛错不能连累另外的监听器，也不能打断采集 */
      }
    });
  }

  function recErr(name, message) {
    const e = new Error(message);
    e.name = name;          // "unsupported" / "denied" / "audio"
    return e;
  }

  function isSupported() {
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }

  /* getUserMedia 只在安全上下文里存在。开发时常用 http://<局域网 IP>:8000
     让手机连过来，那不是安全上下文，navigator.mediaDevices 直接是 undefined ——
     页面如果只说「浏览器不支持录音」，用户会去换浏览器，方向就错了。 */
  function isSecureContext() {
    return window.isSecureContext !== false;
  }

  function isRecording() {
    return !!rec;
  }

  /* 线性重采样到 16kHz，并手工拼 44 字节 WAV 头。 */
  function makeWav(float32, sampleRate) {
    const ratio = sampleRate / TARGET_SAMPLE_RATE;
    const length = Math.floor(float32.length / ratio);
    const pcm = new Int16Array(length);
    for (let i = 0; i < length; i++) {
      const pos = i * ratio;
      const i0 = Math.floor(pos);
      const i1 = Math.min(i0 + 1, float32.length - 1);
      const frac = pos - i0;
      let v = float32[i0] * (1 - frac) + float32[i1] * frac;
      v = Math.max(-1, Math.min(1, v));
      pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
    }

    const buffer = new ArrayBuffer(44 + pcm.length * 2);
    const view = new DataView(buffer);
    const str = (offset, s) => {
      for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
    };
    str(0, "RIFF");
    view.setUint32(4, 36 + pcm.length * 2, true);
    str(8, "WAVE");
    str(12, "fmt ");
    view.setUint32(16, 16, true);          // fmt chunk 长度
    view.setUint16(20, 1, true);           // PCM
    view.setUint16(22, 1, true);           // 单声道
    view.setUint32(24, TARGET_SAMPLE_RATE, true);
    view.setUint32(28, TARGET_SAMPLE_RATE * 2, true);   // 字节率
    view.setUint16(32, 2, true);           // 块对齐
    view.setUint16(34, 16, true);          // 位深
    str(36, "data");
    view.setUint32(40, pcm.length * 2, true);
    new Int16Array(buffer, 44).set(pcm);

    return new Blob([buffer], { type: "audio/wav" });
  }

  /* 多声道混成单声道。讯飞与百度都只收单声道，立体声直接送过去会被判成噪音。 */
  function mixToMono(audioBuffer) {
    const channels = audioBuffer.numberOfChannels;
    if (channels <= 1) return audioBuffer.getChannelData(0);

    const length = audioBuffer.length;
    const out = new Float32Array(length);
    for (let c = 0; c < channels; c++) {
      const data = audioBuffer.getChannelData(c);
      for (let i = 0; i < length; i++) out[i] += data[i];
    }
    for (let i = 0; i < length; i++) out[i] /= channels;
    return out;
  }

  /* decodeAudioData 在老 Safari 上只有回调式、新浏览器返回 Promise，
     两种都接住；重复 resolve 对 Promise 是幂等的。 */
  function decodeAudio(ctx, arrayBuffer) {
    return new Promise((resolve, reject) => {
      let p = null;
      try {
        p = ctx.decodeAudioData(arrayBuffer, resolve, reject);
      } catch (e) {
        reject(e);
        return;
      }
      if (p && typeof p.then === "function") p.then(resolve, reject);
    });
  }

  /**
   * 把用户选的音频文件转成识别接口要的 16kHz 单声道 WAV。
   *
   * 支持哪些格式取决于浏览器自带的解码器：Chrome / Edge / Safari 常见能解
   * mp3、m4a(aac)、wav、ogg、opus、flac、webm、aiff，手机录音（m4a/amr 里的 m4a）
   * 基本都在其中。amr、wma 这类浏览器解不开，调用方 catch 之后仍可把原文件送上去，
   * 由后端判断能否识别。
   *
   * 返回结构与 stop() 一致：{ blob, durationMs, byteLength }。
   */
  async function fileToWav(file) {
    if (!file) throw recErr("decode", "没有选中音频文件");

    const arrayBuffer = await file.arrayBuffer();
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) throw recErr("unsupported", "浏览器不支持音频解码");

    const ctx = new AudioCtx();
    let audio;
    try {
      audio = await decodeAudio(ctx, arrayBuffer);
    } catch (e) {
      throw recErr(
        "decode",
        "这个音频格式浏览器解不开，请换成 MP3、M4A、WAV、OGG 或 FLAC"
      );
    } finally {
      try { ctx.close(); } catch (_) { /* 忽略清理异常 */ }
    }

    const mono = mixToMono(audio);
    if (!mono.length) throw recErr("decode", "音频里没有声音数据");

    return {
      blob: makeWav(mono, audio.sampleRate),
      durationMs: audio.duration * 1000,
      byteLength: mono.length * 2,
    };
  }

  /** 毫秒 → "mm:ss"，与改造前诊断页计时器的输出逐字一致。 */
  function formatDuration(ms) {    const s = Math.floor((ms || 0) / 1000);
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  function teardown() {
    if (!rec) return;
    const { stream, ctx, processor, source, mute } = rec;
    window.clearInterval(timerId);
    timerId = null;
    rec = null;
    try {
      processor.disconnect();
      source.disconnect();
      mute.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      ctx.close();
    } catch (e) {
      /* 忽略清理异常 */
    }
  }

  async function start() {
    if (rec) return;

    if (!isSupported()) {
      throw isSecureContext()
        ? recErr("unsupported", "浏览器不支持录音")
        : recErr("insecure", "录音需要 https 或 localhost 环境");
    }

    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
    } catch (e) {
      throw recErr("denied", (e && e.message) || String(e));
    }

    let ctx = null;
    try {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      ctx = new AudioCtx();

      /* 有些浏览器（Safari，以及 getUserMedia 的 promise 链让出手势后的部分 Chrome）
         会让 AudioContext 停在 suspended：onaudioprocess 一次都不触发，chunks 一直是空，
         用户明明说了话却收到「没有录到声音」—— 与麦克风坏了根本分不出来。 */
      if (ctx.state === "suspended") await ctx.resume();

      const source = ctx.createMediaStreamSource(stream);
      /* 用 ScriptProcessor 而不是 AudioWorklet：后者要求把处理器写在单独的文件里
         用 import 载入，而这个项目刻意没有构建步骤（全是 <script src> 顺序加载）。 */
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      const chunks = [];

      processor.onaudioprocess = (e) => {
        chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
      };

      // 关键：必须接到一个增益为 0 的节点再连到输出。
      // 若直接 processor.connect(ctx.destination)，麦克风采集的声音会从扬声器实时放出来，
      // 形成啸叫/回声，用户会以为程序坏了。
      const mute = ctx.createGain();
      mute.gain.value = 0;
      source.connect(processor);
      processor.connect(mute);
      mute.connect(ctx.destination);

      rec = { stream, ctx, processor, source, mute, chunks, startedAt: Date.now() };
    } catch (e) {
      // 采集链没建起来就把麦克风还回去，否则音轨一直开着，
      // 浏览器标签上的录音指示灯不会灭，用户会以为被偷偷录音
      stream.getTracks().forEach((t) => t.stop());
      if (ctx) {
        try {
          ctx.close();
        } catch (_) {
          /* 忽略清理异常 */
        }
      }
      throw recErr("audio", (e && e.message) || String(e));
    }

    emit("start", 0);
    timerId = window.setInterval(() => {
      if (rec) emit("tick", Date.now() - rec.startedAt);
    }, 250);
  }

  /**
   * 停止并返回录音。
   *   返回 { blob, durationMs, byteLength }；**没有采到任何样本时返回 null**
   *   （由调用方提示「没有录到声音」，本模块不碰 UI）。
   * durationMs 是音频本身的时长（total / sampleRate），用于「时长约 N 秒」的展示；
   * 而 "tick"/"stop" 事件带的是墙上时钟的已录时长，用于屏幕上的计时器。
   */
  async function stop() {
    if (!rec) return null;
    const { ctx, chunks, startedAt } = rec;

    const total = chunks.reduce((n, c) => n + c.length, 0);
    const merged = new Float32Array(total);
    let offset = 0;
    chunks.forEach((c) => {
      merged.set(c, offset);
      offset += c.length;
    });

    const sampleRate = ctx.sampleRate;
    teardown();
    emit("stop", Date.now() - startedAt);

    if (!total) return null;
    return { blob: makeWav(merged, sampleRate), durationMs: (total / sampleRate) * 1000, byteLength: total * 2 };
  }

  /** 同步停止并丢弃，供 beforeunload —— 那里没机会等 Promise，也不需要音频。 */
  function cancel() {
    if (!rec) return;
    const startedAt = rec.startedAt;
    teardown();
    emit("stop", Date.now() - startedAt);
  }

  /** 订阅 "start" | "tick" | "stop"，返回取消订阅的函数。 */
  function on(type, cb) {
    if (!listeners[type]) return () => {};
    listeners[type].push(cb);
    return function off() {
      const i = listeners[type].indexOf(cb);
      if (i >= 0) listeners[type].splice(i, 1);
    };
  }

  window.Recorder = {
    isSupported,
    isSecureContext,
    isRecording,
    start,
    stop,
    cancel,
    fileToWav,
    formatDuration,
    on,
  };
})();
