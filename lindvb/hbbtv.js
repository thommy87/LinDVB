/* LinDVB HbbTV 2.0.4 terminal: the OIPF DAE objects (ETSI TS 102 796 v1.7.1,
 * OIPF DAE vol. 5) that broadcaster applications expect, on top of WebKitGTK.
 *
 * Injected before every page. The terminal (lindvb/ui/hbbtv.py) prepends
 * `window.__lindvbInit = {...}` and talks to this file through
 * window.__lindvb (Python -> JS) and the "lindvb" script message handler
 * (JS -> Python).
 */
(function () {
  "use strict";
  if (window.__lindvb) { return; }
  var init = window.__lindvbInit || {};
  var post = function (msg) {
    try { window.webkit.messageHandlers.lindvb.postMessage(JSON.stringify(msg)); } catch (e) { /* no bridge */ }
  };

  /* ---------------------------------------------------------------- keys */
  var VK = {
    VK_RED: 403, VK_GREEN: 404, VK_YELLOW: 405, VK_BLUE: 406,
    VK_LEFT: 37, VK_UP: 38, VK_RIGHT: 39, VK_DOWN: 40, VK_ENTER: 13, VK_BACK: 461,
    VK_PLAY: 415, VK_PAUSE: 19, VK_PLAY_PAUSE: 402, VK_STOP: 413, VK_FAST_FWD: 417,
    VK_REWIND: 412, VK_TRACK_NEXT: 425, VK_TRACK_PREV: 424, VK_INFO: 457,
    VK_PAGE_UP: 33, VK_PAGE_DOWN: 34, VK_TELETEXT: 459, VK_SUBTITLE: 460,
    VK_CHANNEL_UP: 427, VK_CHANNEL_DOWN: 428
  };
  for (var n = 0; n < 10; n++) { VK["VK_" + n] = 48 + n; }
  window.KeyEvent = window.KeyEvent || {};
  Object.keys(VK).forEach(function (k) {
    try { window.KeyEvent[k] = VK[k]; } catch (e) { /* read-only */ }
    if (!(k in window)) { window[k] = VK[k]; }
  });

  var MASK = { RED: 0x1, GREEN: 0x2, YELLOW: 0x4, BLUE: 0x8, NAVIGATION: 0x10, VCR: 0x20,
               SCROLL: 0x40, INFO: 0x80, NUMERIC: 0x100, ALPHA: 0x200, OTHER: 0x400 };

  function dispatchKey(code, type) {
    var target = document.activeElement || document.body || document;
    var ev;
    try {
      ev = new KeyboardEvent(type, { bubbles: true, cancelable: true });
    } catch (e) {
      ev = document.createEvent("Events");
      ev.initEvent(type, true, true);
    }
    Object.defineProperty(ev, "keyCode", { get: function () { return code; } });
    Object.defineProperty(ev, "which", { get: function () { return code; } });
    Object.defineProperty(ev, "charCode", { get: function () { return type === "keypress" ? code : 0; } });
    return target.dispatchEvent(ev);
  }

  /* ---------------------------------------------------------------- events */
  function eventTarget(obj) {
    var listeners = {};
    obj.addEventListener = obj.addEventListener && obj instanceof Node ? obj.addEventListener :
      function (name, fn) { (listeners[name] = listeners[name] || []).push(fn); };
    obj.__fire = function (name, props, args) {
      var handler = obj["on" + name];
      if (typeof handler === "function") {
        try { handler.apply(obj, args || []); } catch (e) { console.error(e); }
      }
      if (obj instanceof Node) {
        var ev = document.createEvent("Event");
        ev.initEvent(name, false, false);
        Object.keys(props || {}).forEach(function (k) { ev[k] = props[k]; });
        obj.dispatchEvent(ev);
      } else {
        (listeners[name] || []).forEach(function (fn) {
          try { fn.call(obj, props || {}); } catch (e) { console.error(e); }
        });
      }
    };
    return obj;
  }

  /* ---------------------------------------------------------------- channels */
  var ccidCounter = 0;
  function makeChannel(d) {
    d = d || {};
    return {
      ccid: d.ccid || ("ccid:" + (++ccidCounter)), name: d.name || "", onid: d.onid || 0,
      tsid: d.tsid || 0, sid: d.sid || 0, idType: d.idType || 12,
      channelType: d.channelType || 0, majorChannel: d.majorChannel || 0, minorChannel: 0,
      dsd: d.dsd || "", ipBroadcastID: d.ipBroadcastID || "", locked: !!d.locked,
      manualBlock: false, hidden: !!d.hidden, isHD: !!d.isHD, favourite: false,
      favIDs: [], longName: d.name || "", description: "", authorised: true,
      TYPE_TV: 0, TYPE_RADIO: 1, TYPE_OTHER: 2,
      ID_ANALOG: 0, ID_DVB_C: 10, ID_DVB_S: 11, ID_DVB_T: 12, ID_DVB_SI_DIRECT: 13,
      ID_DVB_C2: 14, ID_DVB_S2: 15, ID_DVB_T2: 16, ID_IPTV_SDS: 40, ID_IPTV_URI: 41
    };
  }
  var channels = (init.channels || []).map(makeChannel);
  var current = init.current ? makeChannel(init.current) : null;
  var programmes = init.programmes || [];

  function channelList() {
    var list = channels.slice();
    list.item = function (i) { return list[i] || null; };
    list.getChannel = function (ccid) {
      for (var i = 0; i < list.length; i++) { if (list[i].ccid === ccid) { return list[i]; } }
      return null;
    };
    list.getChannelByTriplet = function (onid, tsid, sid) {
      for (var i = 0; i < list.length; i++) {
        var c = list[i];
        if (c.onid === onid && c.sid === sid && (tsid === null || tsid === undefined || c.tsid === tsid)) { return c; }
      }
      return null;
    };
    list.getChannelBySourceID = function () { return null; };
    return list;
  }

  function programmeObjects() {
    return programmes.map(function (p) {
      return {
        name: p.name, description: p.description, longDescription: p.longDescription,
        startTime: p.startTime, duration: p.duration, programmeID: p.programmeID,
        programmeIDType: p.programmeIDType, channelID: current ? current.ccid : "",
        parentalRatings: p.parentalRatings || [], ID_TVA_CRID: 0, ID_DVB_EVENT: 1,
        ID_TVA_GROUP_CRID: 2, getSIDescriptors: function () { return null; }
      };
    });
  }

  /* ---------------------------------------------------------------- application */
  var keyset = {
    value: 0, otherKeys: [], maximumValue: 0x7FF, maximumOtherKeys: [],
    supportsPointer: false, RED: 0x1, GREEN: 0x2, YELLOW: 0x4, BLUE: 0x8, NAVIGATION: 0x10,
    VCR: 0x20, SCROLL: 0x40, INFO: 0x80, NUMERIC: 0x100, ALPHA: 0x200, OTHER: 0x400,
    setValue: function (value, otherKeys) {
      this.value = value & this.maximumValue;
      this.otherKeys = otherKeys || [];
      post({ cmd: "keyset", value: this.value, otherKeys: this.otherKeys });
      return this.value;
    },
    setPointer: function () { return false; }
  };

  var privateData = {
    keyset: keyset,
    getFreeMem: function () { return 256 * 1024 * 1024; }
  };
  Object.defineProperty(privateData, "currentChannel", { get: function () { return current; } });

  var application = eventTarget({
    visible: true, active: true, isPrimaryReceiver: true, permissions: [],
    privateData: privateData,
    show: function () { this.visible = true; post({ cmd: "show" }); return true; },
    hide: function () { this.visible = false; post({ cmd: "hide" }); return true; },
    activate: function () { this.active = true; },
    deactivate: function () { this.active = false; },
    destroyApplication: function () { post({ cmd: "destroy" }); },
    createApplication: function (uri, createChild) {
      post({ cmd: "create", url: new URL(uri, location.href).href, child: !!createChild });
      return null;
    },
    startWidget: function () { return null; }, stopWidget: function () {},
    getOwnerApplication: function () { return application; }
  });

  var appManager = {
    getOwnerApplication: function () { return application; },
    getChildApplications: function () { return []; },
    getApplicationVisualizationMode: function () { return 1; },
    gc: function () {},
    onLowMemory: null, onApplicationLoaded: null, onApplicationUnloaded: null,
    onApplicationLoadError: null, onApplicationActivated: null
  };

  /* ---------------------------------------------------------------- configuration */
  var cfg = init.config || {};
  var configuration = {
    preferredAudioLanguage: cfg.audioLanguage || "deu",
    preferredSubtitleLanguage: cfg.subtitleLanguage || "deu",
    preferredUILanguage: cfg.uiLanguage || "deu",
    countryId: cfg.country || "DEU", regionId: 0,
    pvrPolicy: 0, pvrSaveEpisodes: 0, pvrSaveDays: 0, pvrStartPadding: 0, pvrEndPadding: 0,
    preferredTimeShiftMode: 0,
    subtitlesEnabled: !!cfg.subtitles, audioDescriptionEnabled: !!cfg.audioDescription,
    clientMetadata: true, timeShiftSynchronized: false, deviceId: cfg.deviceId || "",
    getText: function () { return null; }, setText: function () {},
    requestAccessToDistinctiveIdentifier: function (cb) { if (cb) { setTimeout(function () { cb(false); }, 0); } }
  };
  var localSystem = {
    deviceID: cfg.deviceId || "", systemReady: true, vendorName: "LinDVB", modelName: "LinDVB",
    familyName: "io.github.thommy87.LinDVB", softwareVersion: init.version || "",
    hardwareVersion: "", serialNumber: "", releaseVersion: 0, majorVersion: 0, minorVersion: 0,
    oipfProfile: "", pvrEnabled: false, ciplusEnabled: false, powerState: 1,
    previousPowerState: 1, timeCurrentPowerState: 0, volume: 100, mute: false,
    outputs: [], networkInterfaces: [], tvStandardsSupported: 0, pvrSupport: 0
  };
  var configObject = { configuration: configuration, localSystem: localSystem,
                       getText: configuration.getText, setText: configuration.setText };

  /* ---------------------------------------------------------------- capabilities */
  var capsXml = init.capabilities || "<profilelist/>";
  var capabilities = {
    extraSDVideoDecodes: 0, extraHDVideoDecodes: 0, extraUHDVideoDecodes: 0,
    hasCapability: function (name) { return capsXml.indexOf(name) >= 0; }
  };
  Object.defineProperty(capabilities, "xmlCapabilities", {
    get: function () { return new DOMParser().parseFromString(capsXml, "text/xml"); }
  });

  /* ---------------------------------------------------------------- video/broadcast */
  var broadcasts = [];
  var PLAY = { UNREALIZED: 0, CONNECTING: 1, PRESENTING: 2, STOPPED: 3 };

  function reportRect(el) {
    if (!el.__fullScreen && el.isConnected) {
      var r = el.getBoundingClientRect();
      var hidden = getComputedStyle(el).visibility === "hidden" || r.width === 0 || r.height === 0;
      post({ cmd: "video-rect", x: r.left, y: r.top, w: r.width, h: r.height,
             full: false, hidden: hidden });
    } else {
      post({ cmd: "video-rect", full: true });
    }
  }

  function augmentBroadcast(el) {
    eventTarget(el);
    el.__state = PLAY.UNREALIZED;
    el.__fullScreen = false;
    broadcasts.push(el);
    var props = {
      playState: function () { return el.__state; },
      currentChannel: function () { return el.__state === PLAY.UNREALIZED ? null : current; },
      fullScreen: function () { return el.__fullScreen; },
      programmes: function () { return el.__state === PLAY.PRESENTING ? programmeObjects() : []; },
      error: function () { return el.__error === undefined ? -1 : el.__error; }
    };
    Object.keys(props).forEach(function (k) {
      Object.defineProperty(el, k, { get: props[k], configurable: true });
    });
    function setState(state, error) {
      var old = el.__state;
      el.__state = state;
      el.__error = error;
      if (old !== state || error !== undefined) {
        el.__fire("PlayStateChange", { state: state, error: error }, [state, error]);
      }
    }
    el.__setState = setState;
    el.bindToCurrentChannel = function () {
      if (!current) { setState(PLAY.UNREALIZED, 0); return null; }
      post({ cmd: "bind" });
      if (el.__state !== PLAY.PRESENTING) {
        setState(PLAY.CONNECTING);
        setTimeout(function () { setState(PLAY.PRESENTING); }, 0);
      }
      reportRect(el);
      return current;
    };
    el.setChannel = function (channel, trickplay, contentAccessDescriptorURL, offset) {
      if (channel === null) { el.stop(); setState(PLAY.UNREALIZED); return; }
      setState(PLAY.CONNECTING);
      post({ cmd: "set-channel", ccid: channel.ccid, onid: channel.onid, tsid: channel.tsid,
             sid: channel.sid, quiet: offset === 1 || offset === 2 });
    };
    el.prevChannel = function () { post({ cmd: "channel-step", step: -1 }); };
    el.nextChannel = function () { post({ cmd: "channel-step", step: 1 }); };
    el.stop = function () { post({ cmd: "stop-broadcast" }); setState(PLAY.STOPPED); };
    el.release = function () { post({ cmd: "release-broadcast" }); setState(PLAY.UNREALIZED); };
    el.setFullScreen = function (full) {
      el.__fullScreen = !!full;
      reportRect(el);
      el.__fire("FullScreenChange", {}, []);
    };
    el.getChannelConfig = function () { return channelConfig; };
    el.createChannelObject = function (idType, a, b, c) {
      if (typeof a === "string") { return makeChannel({ idType: idType, dsd: a, sid: b }); }
      return channels.filter(function (x) { return x.onid === a && x.sid === c && (!b || x.tsid === b); })[0] ||
        makeChannel({ idType: idType, onid: a, tsid: b, sid: c });
    };
    el.getVolume = function () { return localSystem.volume; };
    el.setVolume = function (v) { localSystem.volume = v; post({ cmd: "volume", value: v }); return true; };
    el.getComponents = function () { return []; };
    el.getCurrentActiveComponents = function () { return []; };
    el.selectComponent = function () {}; el.unselectComponent = function () {};
    el.addStreamEventListener = function () {}; el.removeStreamEventListener = function () {};
    el.recordNow = function () { return null; }; el.stopRecording = function () {};
    el.pause = function () { return false; }; el.resume = function () { return false; };
    el.setSpeed = function () { return false; }; el.seek = function () { return false; };
    if (window.ResizeObserver) {
      new ResizeObserver(function () { if (el.__state === PLAY.PRESENTING) { reportRect(el); } }).observe(el);
    }
  }

  var channelConfig = {};
  Object.defineProperty(channelConfig, "channelList", { get: channelList });
  Object.defineProperty(channelConfig, "favouriteLists", { get: function () { return []; } });
  Object.defineProperty(channelConfig, "currentFavouriteList", { get: function () { return null; } });
  channelConfig.createFilteredList = function () { return channelList(); };
  channelConfig.startScan = function () {}; channelConfig.stopScan = function () {};

  /* ---------------------------------------------------------------- A/V control object */
  var AV_TYPES = ["video/mp4", "video/mpeg", "video/mpeg4", "audio/mp4", "audio/mpeg",
                  "application/dash+xml", "video/mp2t", "audio/mp3"];
  var AV = { STOPPED: 0, PLAYING: 1, PAUSED: 2, CONNECTING: 3, BUFFERING: 4, FINISHED: 5, ERROR: 6 };

  function augmentAV(el) {
    eventTarget(el);
    var video = document.createElement("video");
    video.style.cssText = "width:100%;height:100%;display:block;background:transparent";
    video.setAttribute("playsinline", "");
    el.appendChild(video);
    var data = el.getAttribute("data") || "";
    el.removeAttribute("data");
    var state = AV.STOPPED, speed = 0, error = -1;
    function setState(s, err) {
      if (s === state && err === undefined) { return; }
      state = s;
      if (err !== undefined) { error = err; }
      el.__fire("PlayStateChange", { state: s }, [s]);
    }
    Object.defineProperty(el, "data", { configurable: true,
      get: function () { return data; },
      set: function (v) { data = v ? new URL(v, location.href).href : ""; video.removeAttribute("src"); setState(AV.STOPPED); } });
    var getters = {
      playState: function () { return state; },
      playPosition: function () { return Math.round(video.currentTime * 1000); },
      playTime: function () { return isFinite(video.duration) ? Math.round(video.duration * 1000) : 0; },
      speed: function () { return speed; },
      error: function () { return error; },
      nrTracks: function () { return 1; }, currentTrackIndex: function () { return 0; }
    };
    Object.keys(getters).forEach(function (k) {
      Object.defineProperty(el, k, { get: getters[k], configurable: true });
    });
    var mediaType = (el.getAttribute("type") || "").toLowerCase();
    el.play = function (s) {
      s = (s === undefined) ? 1 : s;
      if (!data) { return false; }
      if (s === 0) { video.pause(); speed = 0; setState(AV.PAUSED); return true; }
      if (video.getAttribute("src") !== data) {
        setState(AV.CONNECTING);
        post({ cmd: "av-play", url: data, type: mediaType });
        video.src = data;
      }
      speed = s;
      video.playbackRate = Math.abs(s) >= 1 ? Math.min(Math.abs(s), 16) : 1;
      var p = video.play();
      if (p && p.catch) { p.catch(function () { setState(AV.ERROR, 4); }); }
      return true;
    };
    el.stop = function () {
      video.pause(); video.removeAttribute("src"); video.load(); speed = 0; setState(AV.STOPPED);
      post({ cmd: "av-stop" });
      return true;
    };
    el.seek = function (ms) { try { video.currentTime = ms / 1000; return true; } catch (e) { return false; } };
    el.setVolume = function (v) { video.volume = Math.max(0, Math.min(1, v / 100)); return true; };
    el.setFullScreen = function () {};
    el.queue = function () { return false; };
    el.getComponents = function () { return []; };
    el.selectComponent = function () {}; el.unselectComponent = function () {};
    video.addEventListener("playing", function () { setState(AV.PLAYING); });
    video.addEventListener("waiting", function () { setState(AV.BUFFERING); });
    video.addEventListener("pause", function () { if (state === AV.PLAYING) { setState(AV.PAUSED); } });
    video.addEventListener("ended", function () { setState(AV.FINISHED); });
    video.addEventListener("error", function () { setState(AV.ERROR, 0); });
    video.addEventListener("timeupdate", function () { el.__fire("PlayPositionChanged", {}, [getters.playPosition()]); });
  }

  /* ---------------------------------------------------------------- objects */
  var SIMPLE = {
    "application/oipfapplicationmanager": function (el) {
      el.getOwnerApplication = appManager.getOwnerApplication;
      el.getChildApplications = appManager.getChildApplications;
      el.getApplicationVisualizationMode = appManager.getApplicationVisualizationMode;
      el.gc = appManager.gc;
    },
    "application/oipfconfiguration": function (el) {
      el.configuration = configuration; el.localSystem = localSystem;
      el.getText = configuration.getText; el.setText = configuration.setText;
    },
    "application/oipfcapabilities": function (el) {
      el.hasCapability = capabilities.hasCapability;
      el.extraSDVideoDecodes = 0; el.extraHDVideoDecodes = 0; el.extraUHDVideoDecodes = 0;
      Object.defineProperty(el, "xmlCapabilities", { get: function () { return capabilities.xmlCapabilities; } });
    },
    "application/oipfparentalcontrolmanager": function (el) {
      el.parentalRatingSchemes = [];
      el.isPINEntryLocked = false;
      el.getBlockUnrated = function () { return false; };
      el.verifyParentalControlPIN = function () { return 1; };
    },
    "application/oipfsearchmanager": function (el) {
      el.guideDaysAvailable = 0;
      el.createSearch = function () { return null; };
      el.getChannelConfig = function () { return channelConfig; };
    },
    "video/broadcast": augmentBroadcast
  };
  AV_TYPES.forEach(function (t) { SIMPLE[t] = augmentAV; });

  function augment(el) {
    if (el.__lindvb || el.tagName !== "OBJECT") { return; }
    var type = (el.getAttribute("type") || "").toLowerCase();
    var fn = SIMPLE[type];
    if (!fn) { return; }
    el.__lindvb = true;
    try { fn(el); } catch (e) { console.error("LinDVB HbbTV:", type, e); }
  }

  function scan(root) {
    if (root.tagName === "OBJECT") { augment(root); }
    if (root.querySelectorAll) {
      var list = root.querySelectorAll("object");
      for (var i = 0; i < list.length; i++) { augment(list[i]); }
    }
  }

  new MutationObserver(function (records) {
    records.forEach(function (r) {
      if (r.type === "attributes") { augment(r.target); }
      for (var i = 0; i < r.addedNodes.length; i++) {
        if (r.addedNodes[i].nodeType === 1) { scan(r.addedNodes[i]); }
      }
    });
  }).observe(document, { childList: true, subtree: true, attributes: true, attributeFilter: ["type"] });
  document.addEventListener("DOMContentLoaded", function () { scan(document); });

  window.oipfObjectFactory = {
    isObjectSupported: function (t) { return !!SIMPLE[(t || "").toLowerCase()]; },
    createApplicationManagerObject: function () { return appManager; },
    createConfigurationObject: function () { return configObject; },
    createCapabilitiesObject: function () { return capabilities; },
    createVideoBroadcastObject: function () {
      var el = document.createElement("object");
      el.setAttribute("type", "video/broadcast"); augment(el); return el;
    },
    createVideoMpegObject: function () {
      var el = document.createElement("object");
      el.setAttribute("type", "video/mpeg"); augment(el); return el;
    },
    createParentalControlManagerObject: function () {
      var el = document.createElement("object");
      el.setAttribute("type", "application/oipfParentalControlManager"); augment(el); return el;
    },
    createSearchManagerObject: function () {
      var el = document.createElement("object");
      el.setAttribute("type", "application/oipfSearchManager"); augment(el); return el;
    }
  };

  /* An app's window.close() ends the app (TS 102 796 6.2.2.10). */
  window.close = function () { application.destroyApplication(); };

  /* ---------------------------------------------------------------- terminal side */
  window.__lindvb = {
    key: function (code) {
      var ok = dispatchKey(code, "keydown");
      dispatchKey(code, "keypress");
      dispatchKey(code, "keyup");
      return ok;
    },
    channelChanged: function (ch, error) {
      if (error !== undefined && error !== null) {
        broadcasts.forEach(function (b) {
          b.__fire("ChannelChangeError", { channel: ch, errorState: error }, [ch, error]);
          b.__setState(PLAY.UNREALIZED, error);
        });
        return;
      }
      current = ch ? makeChannel(ch) : null;
      programmes = [];
      broadcasts.forEach(function (b) {
        if (b.__state === PLAY.UNREALIZED) { return; }
        b.__setState(PLAY.PRESENTING);
        b.__fire("ChannelChangeSucceeded", { channel: current }, [current]);
      });
    },
    programmes: function (list) {
      programmes = list || [];
      broadcasts.forEach(function (b) { b.__fire("ProgrammesChanged", {}, []); });
    },
    channels: function (list) { channels = (list || []).map(makeChannel); },
    broadcastState: function (state, error) {
      broadcasts.forEach(function (b) { b.__setState(state, error); });
    },
    keyset: keyset
  };
  post({ cmd: "ready", url: location.href });
})();
