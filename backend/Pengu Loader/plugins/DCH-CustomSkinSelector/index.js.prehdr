(function createCustomSkinSelector() {
  const LOG_PREFIX = "[DCH-CustomSkinSelector]";
  const BUTTON_CLASS = "lu-custom-skin-button";
  const BUTTON_SELECTOR = `.${BUTTON_CLASS}`;
  const PANEL_CLASS = "lu-custom-skin-panel";
  const PANEL_ID = "lu-custom-skin-panel-container";
  const EVENT_SKIN_STATE = "lu-skin-monitor-state";
  const REQUEST_TYPE = "request-skin-mods";
  const BUTTON_ICON_ASSET_PATH = "button-skin.png";

  // D-Changer's menu language (DCH-I18n); English until it has loaded
  const t = (text, vars) =>
    window.DChangerI18n
      ? window.DChangerI18n.t(text, vars)
      : text.replace(/\{(\w+)\}/g, (m, k) => (vars && k in vars ? String(vars[k]) : m));

  let bridge = null;
  let skinMonitorState = null;
  let championLocked = false;
  let initialized = false;
  let selectedModId = null;
  let selectedModSkinId = null;
  let modsForCurrentSkin = [];
  // "championId:skinId" the mods list was answered for
  let modsForKey = null;
  let pythonChromaState = null;
  let currentPhase = null;
  let panel = null;
  let panelButtonRef = null;
  let panelSkinItemRef = null;
  let customButtonIconUrl = null;
  let selectionRequestCounter = 0;
  let pendingSelectionRequest = null;
  let lastSkinModsRequestKey = null;
  let lastSkinModsRequestAt = 0;
  // Champion the preflight verdicts below belong to.
  let preflightForKey = null;
  let preflightRequestedAt = 0;
  // skinId -> { kind, code, message } for every skin known to disappoint.
  const skinVerdicts = new Map();
  // skinId whose warning banner is on screen, and the banner itself.
  let bannerSkinId = null;
  let bannerHost = null;
  let bannerTimer = null;

  function logInfo(message, extra) {
    console.log(`${LOG_PREFIX} ${message}`, extra ?? "");
  }

  function applyCustomButtonIcon() {
    if (!customButtonIconUrl) return;

    document.querySelectorAll(`${BUTTON_SELECTOR} .content`).forEach((content) => {
      content.style.backgroundImage = `url('${customButtonIconUrl}')`;
      content.style.backgroundRepeat = "no-repeat";
      content.style.backgroundPosition = "center";
      content.style.backgroundSize = "contain";
    });
  }

  function handleLocalAssetUrl(data) {
    const assetPath = String(data?.assetPath || "").trim();
    if (assetPath !== BUTTON_ICON_ASSET_PATH) return;

    const url = typeof data?.url === "string" ? data.url.replace("localhost", "127.0.0.1") : "";
    if (!url) return;

    customButtonIconUrl = url;
    applyCustomButtonIcon();
  }

  function waitForBridge() {
    return new Promise((resolve, reject) => {
      const timeout = 10000;
      const interval = 50;
      let elapsed = 0;

      const check = () => {
        if (window.__dchangerBridge) {
          resolve(window.__dchangerBridge);
          return;
        }
        elapsed += interval;
        if (elapsed >= timeout) {
          reject(new Error("Bridge not available"));
          return;
        }
        setTimeout(check, interval);
      };

      check();
    });
  }

  function getSkinOffset(skinItem) {
    if (!skinItem) return null;
    const directMatch = skinItem.className.match(/skin-carousel-offset-(\d+)/);
    if (directMatch) return Number(directMatch[1]);
    const nested = skinItem.querySelector("[class*='skin-carousel-offset-']");
    if (!nested) return null;
    const nestedMatch = nested.className.match(/skin-carousel-offset-(\d+)/);
    return nestedMatch ? Number(nestedMatch[1]) : null;
  }

  function isCurrentSkinItem(skinItem) {
    if (!skinItem) return false;

    if (skinItem.classList.contains("skin-selection-item")) {
      const explicitlyActive = (
        skinItem.classList.contains("active-skin") ||
        skinItem.classList.contains("selected") ||
        skinItem.getAttribute("aria-selected") === "true"
      );
      return explicitlyActive || getSkinOffset(skinItem) === 2;
    }

    if (skinItem.classList.contains("thumbnail-wrapper")) {
      return (
        skinItem.classList.contains("active-skin") ||
        skinItem.classList.contains("selected") ||
        skinItem.getAttribute("aria-selected") === "true"
      );
    }

    return false;
  }

  function getSkinIdFromItem(skinItem) {
    if (!skinItem) return null;

    const candidates = [
      skinItem.dataset?.skinId,
      skinItem.dataset?.id,
      skinItem.getAttribute("data-skin-id"),
      skinItem.getAttribute("data-id"),
    ];

    for (const candidate of candidates) {
      const parsed = Number(candidate);
      if (Number.isFinite(parsed) && parsed > 0) {
        return parsed;
      }
    }

    return null;
  }

  function doesSkinItemMatchSkinState(skinItem) {
    const currentSkinId = Number(skinMonitorState?.skinId);
    if (!Number.isFinite(currentSkinId) || currentSkinId <= 0) {
      return true;
    }

    const itemSkinId = getSkinIdFromItem(skinItem);
    if (!Number.isFinite(itemSkinId) || itemSkinId <= 0) {
      return true;
    }

    return itemSkinId === currentSkinId;
  }

  function normalizeModId(mod) {
    return String(mod?.relativePath || mod?.modName || "");
  }

  function visibleModName(mod, fallback = "") {
    const alias = typeof mod?.displayName === "string" ? mod.displayName.trim() : "";
    if (alias) return alias;
    return mod?.modName || fallback;
  }

  function isModAvailableForSkin(mod, skinId = getCurrentSkinContext().skinId) {
    if (!mod || mod._none) return false;
    const requestedSkinId = Number(skinId);
    if (!Number.isFinite(requestedSkinId)) return false;
    if (mod.availableForRequestedSkin === true) return true;

    const targetSkinIds = Array.isArray(mod.targetSkinIds)
      ? mod.targetSkinIds.map(Number).filter(Number.isFinite)
      : [];
    if (targetSkinIds.includes(requestedSkinId)) return true;

    return mod.availableForRequestedSkin == null && Number(mod.skinId) === requestedSkinId;
  }

  function isModSelected(mod, skinId = getCurrentSkinContext().skinId) {
    const requestedSkinId = Number(skinId);
    const selectedSkinId = Number(selectedModSkinId);
    const hasSelectionForSkin = Boolean(selectedModId) &&
      Number.isFinite(requestedSkinId) &&
      Number.isFinite(selectedSkinId) &&
      selectedSkinId === requestedSkinId;
    if (mod?._none) return !hasSelectionForSkin;
    return Boolean(selectedModId) &&
      selectedModId === normalizeModId(mod) &&
      hasSelectionForSkin;
  }

  function getCurrentSkinContext() {
    const championId = Number(skinMonitorState?.championId);
    let skinId = Number(skinMonitorState?.skinId);
    const selectedChromaId = Number(pythonChromaState?.selectedChromaId);
    const currentSkinId = Number(pythonChromaState?.currentSkinId);

    const chromaBelongsToChampion =
      Number.isFinite(selectedChromaId) &&
      selectedChromaId > 0 &&
      Number.isFinite(championId) &&
      championId > 0 &&
      Math.floor(selectedChromaId / 1000) === championId;
    const chromaContextMatches =
      !Number.isFinite(currentSkinId) ||
      currentSkinId <= 0 ||
      Math.floor(currentSkinId / 1000) === championId;

    if (chromaBelongsToChampion && chromaContextMatches) {
      skinId = selectedChromaId;
    }

    return {
      championId: Number.isFinite(championId) ? championId : null,
      skinId: Number.isFinite(skinId) ? skinId : null,
      skinName: String(skinMonitorState?.name || "Unknown Skin"),
    };
  }

  function handleChromaStateUpdate(data) {
    pythonChromaState = data && typeof data === "object" ? data : null;
    requestModsForCurrentSkin();
    scanSkinSelection();
  }

  function resetStaleChromaStateForSkin(skinId) {
    if (!pythonChromaState) return;

    const incomingSkinId = Number(skinId);
    const selectedChromaId = Number(pythonChromaState.selectedChromaId);
    const currentSkinId = Number(pythonChromaState.currentSkinId);
    if (!Number.isFinite(incomingSkinId) || incomingSkinId <= 0) return;

    const belongsToPreviousChromaContext =
      incomingSkinId === selectedChromaId ||
      incomingSkinId === currentSkinId;
    if (!belongsToPreviousChromaContext) {
      pythonChromaState = null;
    }
  }

  function resetCustomSkinSessionState() {
    // The skin state of the previous champ select must not answer for this one
    skinMonitorState = null;
    lastSkinModsRequestKey = null;
    modsForKey = null;
    pythonChromaState = null;
    selectedModId = null;
    selectedModSkinId = null;
    pendingSelectionRequest = null;
    modsForCurrentSkin = [];
  }

  function handlePhaseChange(data) {
    const phase = String(data?.phase || "");
    const previousPhase = currentPhase;
    currentPhase = phase;

    if (
      phase === "ChampSelect" &&
      previousPhase &&
      previousPhase !== "ChampSelect"
    ) {
      resetCustomSkinSessionState();
    } else if (
      phase !== "ChampSelect" &&
      previousPhase === "ChampSelect"
    ) {
      resetCustomSkinSessionState();
    }
  }

  function createSelectionRequestId() {
    selectionRequestCounter += 1;
    return `${LOG_PREFIX}-${Date.now()}-${selectionRequestCounter}`;
  }

  function injectCSS() {
    const styleId = "lu-custom-skin-selector-style";
    if (document.getElementById(styleId)) return;

    const styleTag = document.createElement("style");
    styleTag.id = styleId;
    styleTag.textContent = getPluginCSS();
    document.head.appendChild(styleTag);
  }

  function getPluginCSS() {
    const btn = BUTTON_CLASS;
    const pnl = PANEL_CLASS;
    return `
.${btn} {
  pointer-events: auto; -webkit-user-select: none; list-style-type: none; cursor: pointer;
  display: block !important; bottom: 1px; height: 25px; left: 50%; position: absolute;
  transform: translateX(-50%) translateY(-205%); width: 25px; z-index: 10; direction: ltr;
}
.${btn}[data-hidden], .${btn}[data-hidden] * {
  pointer-events: none !important; cursor: default !important; visibility: hidden !important;
}
[data-dch-blocked="1"] { position: relative; cursor: not-allowed !important; }
[data-dch-blocked="1"]::after {
  content: ""; position: absolute; inset: 0; pointer-events: none; z-index: 6;
  background: linear-gradient(0deg, rgba(200,40,40,.30), rgba(200,40,40,.10));
  box-shadow: inset 0 0 0 2px #c83232; border-radius: inherit;
}
.dch-broken-badge {
  position: absolute; top: 2px; right: 2px; z-index: 11; pointer-events: none;
  font: 700 9px/1 system-ui, sans-serif; letter-spacing: .3px;
  color: #fff; background: #c83232; border-radius: 3px;
  padding: 3px 5px; box-shadow: 0 1px 4px rgba(0,0,0,.5); text-transform: uppercase;
}
.dch-noaudio-badge {
  position: absolute; top: 2px; right: 2px; z-index: 11; pointer-events: none;
  font: 700 9px/1 system-ui, sans-serif; letter-spacing: .3px;
  color: #1e2328; background: #c8aa6e; border-radius: 3px;
  padding: 3px 5px; box-shadow: 0 1px 4px rgba(0,0,0,.5); text-transform: uppercase;
}
.dch-skin-warning-banner {
  position: fixed; left: 50%; bottom: 96px; transform: translateX(-50%);
  z-index: 2147483000; width: min(520px, 86vw);
  background: #1e2328; border: 1px solid #c8aa6e; border-radius: 6px;
  box-shadow: 0 8px 28px rgba(0,0,0,.65); color: #cdbe91;
  font: 400 12px/1.5 system-ui, sans-serif; padding: 12px 14px;
  direction: ltr; text-align: left;
}
.dch-skin-warning-banner .dch-warn-title {
  font-weight: 700; color: #c8aa6e; text-transform: uppercase;
  letter-spacing: .6px; font-size: 11px; margin-bottom: 6px;
}
.dch-skin-warning-banner .dch-warn-body { color: #a09b8c; margin-bottom: 10px; }
.dch-skin-warning-banner .dch-warn-body strong { color: #f0e6d2; }
.dch-skin-warning-banner .dch-warn-actions { display: flex; gap: 8px; justify-content: flex-end; }
.dch-skin-warning-banner button {
  font: 700 11px/1 system-ui, sans-serif; letter-spacing: .4px; text-transform: uppercase;
  padding: 8px 14px; border-radius: 3px; cursor: pointer;
  border: 1px solid #785a28; background: #1e2328; color: #cdbe91;
}
.dch-skin-warning-banner button.dch-warn-primary {
  background: linear-gradient(180deg,#c8aa6e,#785a28); border-color: #c8aa6e; color: #1e2328;
}
.dch-skin-warning-banner button:hover { filter: brightness(1.15); }
.${btn} .outer-mask {
  pointer-events: auto; cursor: pointer; border-radius: 50%;
  box-shadow: 0 0 4px 1px rgba(1,10,19,.25); box-sizing: border-box; height: 100%;
  overflow: hidden; position: relative;
}
.${btn} .frame-color {
  pointer-events: auto; cursor: default;
  background-image: linear-gradient(0deg,#695625 0,#a9852d 23%,#b88d35 93%,#c8aa6e);
  box-sizing: border-box; height: 100%; overflow: hidden; width: 100%; padding: 2px;
}
.${btn} .content {
  pointer-events: auto; cursor: pointer; display: block; background-color: #1e2328;
  background-repeat: no-repeat; background-position: center; background-size: contain;
  border: 2px solid #010a13; border-radius: 50%; height: 16px; margin: 1px; width: 16px;
}
.${btn} .inner-mask {
  cursor: default; border-radius: 50%; box-sizing: border-box; overflow: hidden;
  pointer-events: none; position: absolute;
  box-shadow: inset 0 0 4px 4px rgba(0,0,0,.75);
  width: calc(100% - 4px); height: calc(100% - 4px); left: 2px; top: 2px;
}
.thumbnail-wrapper.active-skin, .skin-selection-item { position: relative; }
.thumbnail-wrapper .${btn} {
  direction: ltr; background: transparent; cursor: pointer; height: 28px; width: 28px;
  bottom: 1px; left: 50%; position: absolute;
  transform: translateX(-50%) translateY(-205%); z-index: 10;
}
.thumbnail-wrapper .${btn} .outer-mask { display: block; }
.thumbnail-wrapper .${btn} .content { transform: translate(1px,1px); }
.${pnl} { position: fixed; z-index: 10000; pointer-events: all; -webkit-user-select: none; }
.${pnl}[data-no-button] { pointer-events: none; cursor: default !important; }
.${pnl}[data-no-button] * { pointer-events: none !important; cursor: default !important; }
.${pnl} .flyout { position: absolute; overflow: visible; pointer-events: all; -webkit-user-select: none; }
.${pnl}[data-no-button] .flyout { pointer-events: none !important; cursor: default !important; }
.${pnl} .flyout-frame { position: relative; transition: 250ms all cubic-bezier(0.02,0.85,0.08,0.99); }
.${pnl} .flyout .caret, .${pnl} .flyout [class*="caret"],
.${pnl} lol-uikit-flyout-frame .caret, .${pnl} lol-uikit-flyout-frame [class*="caret"],
.${pnl} .flyout::part(caret), .${pnl} lol-uikit-flyout-frame::part(caret) {
  z-index: 3 !important; position: relative;
}
.${pnl} .chroma-modal { background: #000; display: flex; flex-direction: column; width: 305px; position: relative; z-index: 0; }
.${pnl} .chroma-modal.chroma-view { min-height: 355px; max-height: 420px; }
.${pnl} .champ-select-chroma-modal { background: #000; display: flex; flex-direction: column; width: 305px; position: relative; z-index: 0; }
.${pnl} .champ-select-chroma-modal.chroma-view { min-height: 0; max-height: 420px; }
.${pnl} .border {
  position: absolute; top: 0; left: 0; box-sizing: border-box; background-color: transparent;
  box-shadow: 0 0 0 1px rgba(1,10,19,0.48); transition: 250ms all cubic-bezier(0.02,0.85,0.08,0.99);
  border-top: 2px solid transparent; border-left: 2px solid transparent;
  border-right: 2px solid transparent; border-bottom: none;
  border-image: linear-gradient(to top,#785a28 0,#463714 50%,#463714 100%) 1 stretch;
  border-image-slice: 1 1 0 1; width: 100%; height: 100%; visibility: visible; z-index: 2; pointer-events: none;
}
.${pnl} .lc-flyout-content { position: relative; }
.${pnl} .chroma-information {
  border-bottom: thin solid #463714; flex-grow: 0;
  overflow: hidden; position: relative; width: 100%; z-index: 1;
}
.${pnl} .chroma-information[data-has-preview="false"] { display: none; }
.${pnl} .chroma-information-image {
  bottom: 0; display: block; height: auto; left: 0; position: absolute; width: 100%;
}
.${pnl} .child-skin-name {
  bottom: 10px; color: #f7f0de;
  font-family: "LoL Display","Times New Roman",Times,Baskerville,Georgia,serif;
  font-size: 24px; font-weight: 700; position: absolute; text-align: center; width: 100%;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  text-shadow: -1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 1px 1px 0 #000, 0 0 4px #000;
}
.${pnl} .standalone-skin-name {
  bottom: auto; box-sizing: border-box; display: none; padding: 10px 12px 8px; position: relative;
}
.${pnl} .champ-select-chroma-modal[data-has-preview="false"] > .standalone-skin-name { display: block; }
.${pnl} .chroma-selection {
  pointer-events: all; height: 100%; overflow: auto; transform: translateZ(0);
  -webkit-mask-box-image-source: url("/fe/lol-static-assets/images/uikit/scrollable/scrollable-content-gradient-mask-bottom.png");
  -webkit-mask-box-image-slice: 0 8 18 0 fill;
  align-items: center; display: flex; flex-direction: row; flex-grow: 0; flex-wrap: wrap;
  justify-content: center; max-height: 92px; min-height: 40px; padding: 7px 0;
  width: 100%; position: relative; z-index: 1;
}
.${pnl}[data-no-button] .chroma-selection { pointer-events: none; cursor: default; }
.${pnl} .chroma-selection ul { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; justify-content: center; }
.${pnl} .chroma-selection li { list-style: none; margin: 2px 4px; padding: 0; display: flex; align-items: center; justify-content: center; }
.${pnl} .chroma-skin-button {
  pointer-events: all; align-items: center; border-radius: 50%; box-shadow: 0 0 2px #010a13;
  border: none; display: flex; height: 26px; width: 26px; min-width: 26px; min-height: 26px;
  max-width: 26px; max-height: 26px; aspect-ratio: 1/1; justify-content: center; margin: 0;
  padding: 0; cursor: pointer; box-sizing: border-box; background: transparent !important;
  background-color: transparent !important; flex: 0 0 26px; transform: scale(1);
}
.${pnl}[data-no-button] .chroma-skin-button { pointer-events: none !important; cursor: default !important; }
.${pnl} .chroma-skin-button:not(.locked) { cursor: pointer; opacity: 1 !important; }
.${pnl} .chroma-skin-button.locked { opacity: 1 !important; cursor: pointer; }
.${pnl} .chroma-skin-button .contents {
  pointer-events: all; align-items: center; border: 2px solid #010a13; border-radius: 50%;
  display: flex; height: 18px; width: 18px; min-width: 18px; min-height: 18px;
  max-width: 18px; max-height: 18px; aspect-ratio: 1/1; justify-content: center;
  background: linear-gradient(135deg,#27211C 0,#27211C 50%,#27211C 50%,#27211C 100%);
  box-shadow: 0 0 0 2px transparent; background-size: cover; background-position: center;
  background-repeat: no-repeat; background-color: #1e2328; transform: scale(1);
}
.${pnl} .chroma-skin-button.selected .contents, .${pnl} .chroma-skin-button:hover .contents {
  box-shadow: 0 0 0 2px #c89b3c; transform: scale(1);
}
.${pnl} .chroma-skin-button.locked:hover:not([purchase-disabled]) { opacity: 1 !important; }
.${pnl} .chroma-skin-button.locked.purchase-disabled { opacity: 1 !important; pointer-events: none; }
`;
  }

  function hasOtherPluginButtons(skinItem) {
    return skinItem.querySelector(':scope > .lu-chroma-button, :scope > .lu-forms-button, :scope > [class*="chroma-button"], :scope > [class*="forms-button"]') !== null;
  }

  function createFakeButton() {
    const button = document.createElement("div");
    button.className = BUTTON_CLASS;

    const outerMask = document.createElement("div");
    outerMask.className = "outer-mask interactive";

    const frameColor = document.createElement("div");
    frameColor.className = "frame-color";

    const content = document.createElement("div");
    content.className = "content";

    const innerMask = document.createElement("div");
    innerMask.className = "inner-mask inner-shadow";

    frameColor.appendChild(content);
    frameColor.appendChild(innerMask);
    outerMask.appendChild(frameColor);
    button.appendChild(outerMask);

    if (customButtonIconUrl) {
      content.style.backgroundImage = `url('${customButtonIconUrl}')`;
      content.style.backgroundRepeat = "no-repeat";
      content.style.backgroundPosition = "center";
      content.style.backgroundSize = "contain";
    }

    button.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();

      const skinItem = button.closest(".skin-selection-item, .thumbnail-wrapper");
      if (!skinItem) return;
      if (!modsForCurrentSkin.length) return;

      togglePanel(button, skinItem);
    });

    button.addEventListener("mousedown", (event) => {
      event.stopPropagation();
    });

    return button;
  }

  function updateButtonVisibility(button, shouldShow) {
    if (!button) return;

    if (shouldShow) {
      button.style.display = "block";
      button.style.visibility = "visible";
      button.style.pointerEvents = "auto";
      button.style.opacity = "1";
      button.style.cursor = "pointer";
      button.removeAttribute("data-hidden");
    } else {
      button.style.display = "none";
      button.style.visibility = "hidden";
      button.style.pointerEvents = "none";
      button.style.opacity = "0";
      button.style.cursor = "default";
      button.setAttribute("data-hidden", "true");
      if (panel && panel.parentNode && panelButtonRef === button) {
        closePanel();
      }
    }
  }

  function removeAllButtons() {
    document.querySelectorAll(BUTTON_SELECTOR).forEach((node) => node.remove());
  }

  function sendDeselect(expectedModId = selectedModId) {
    const { championId, skinId } = getCurrentSkinContext();
    if (!bridge || !championId || !skinId) return;

    const requestId = createSelectionRequestId();
    pendingSelectionRequest = { requestId, operation: "deselect" };
    bridge.send({
      type: "select-skin-mod",
      championId,
      skinId,
      modId: null,
      expectedModId,
      requestId,
    });
  }

  function sendSelect(modId, modData) {
    const { championId, skinId } = getCurrentSkinContext();
    if (!bridge || !championId || !skinId) return;

    const requestId = createSelectionRequestId();
    pendingSelectionRequest = { requestId, operation: "select", modId };
    bridge.send({ type: "select-skin-mod", championId, skinId, modId, modData, requestId });
  }

  function requestModsForCurrentSkin() {
    if (!bridge) return;
    const { championId, skinId } = getCurrentSkinContext();

    if (!championLocked || !championId || !skinId) {
      modsForCurrentSkin = [];
      scanSkinSelection();
      return;
    }

    const requestKey = `${championId}:${skinId}`;
    if (requestKey !== modsForKey) {
      // Until Python answers, the list on screen belongs to another skin
      modsForCurrentSkin = [];
      modsForKey = null;
      scanSkinSelection();
    }
    const now = Date.now();
    if (
      requestKey === lastSkinModsRequestKey &&
      now - lastSkinModsRequestAt < 750
    ) {
      return;
    }
    lastSkinModsRequestKey = requestKey;
    lastSkinModsRequestAt = now;

    bridge.send({ type: REQUEST_TYPE, championId, skinId });
  }

  function createPanelForMods(mods, buttonElement) {
    if (!buttonElement || !mods.length) return null;

    const existing = document.getElementById(PANEL_ID);
    if (existing) existing.remove();

    const root = document.createElement("div");
    root.id = PANEL_ID;
    root.className = PANEL_CLASS;
    root.style.position = "fixed";
    root.style.top = "0";
    root.style.left = "0";
    root.style.width = "100%";
    root.style.height = "100%";
    root.style.pointerEvents = "none";

    let flyout;
    try {
      flyout = document.createElement("lol-uikit-flyout-frame");
      flyout.className = "flyout";
      flyout.setAttribute("orientation", "top");
      flyout.setAttribute("animated", "false");
      flyout.setAttribute("caretoffset", "undefined");
      flyout.setAttribute("borderless", "undefined");
      flyout.setAttribute("caretless", "undefined");
      flyout.setAttribute("show", "true");
    } catch {
      flyout = document.createElement("div");
      flyout.className = "flyout";
    }
    flyout.style.position = "absolute";
    flyout.style.overflow = "visible";
    flyout.style.pointerEvents = "all";

    let flyoutContent;
    try {
      flyoutContent = document.createElement("lc-flyout-content");
    } catch {
      flyoutContent = document.createElement("div");
      flyoutContent.className = "lc-flyout-content";
    }

    const modal = document.createElement("div");
    modal.className = "champ-select-chroma-modal chroma-view ember-view";

    const border = document.createElement("div");
    border.className = "border";

    const chromaInfo = document.createElement("div");
    chromaInfo.className = "chroma-information";

    const preview = document.createElement("img");
    preview.className = "chroma-information-image";

    const skinName = document.createElement("div");
    skinName.className = "child-skin-name";
    skinName.textContent = getCurrentSkinContext().skinName;

    const standaloneSkinName = document.createElement("div");
    standaloneSkinName.className = "child-skin-name standalone-skin-name";
    standaloneSkinName.textContent = skinName.textContent;

    chromaInfo.appendChild(preview);
    chromaInfo.appendChild(skinName);

    const scrollable = document.createElement("div");
    scrollable.className = "chroma-selection";

    const list = document.createElement("ul");
    let previewLoadToken = 0;

    const setPreviewImage = (url, label) => {
      previewLoadToken += 1;
      const loadToken = previewLoadToken;
      const hasPreview = Boolean(url);
      const nextLabel = label || getCurrentSkinContext().skinName;

      skinName.textContent = nextLabel;
      standaloneSkinName.textContent = nextLabel;

      if (!hasPreview) {
        modal.dataset.hasPreview = "false";
        chromaInfo.dataset.hasPreview = "false";
        preview.removeAttribute("src");
        chromaInfo.style.height = "";
        if (panel && panelButtonRef) requestAnimationFrame(() => positionPanel(panel, panelButtonRef));
        return;
      }

      const loader = new Image();
      loader.addEventListener("load", () => {
        if (loadToken !== previewLoadToken) return;
        const width = chromaInfo.clientWidth || 305;
        if (loader.naturalWidth > 0) {
          chromaInfo.style.height = `${loader.naturalHeight * (width / loader.naturalWidth)}px`;
        }
        preview.src = url;
        modal.dataset.hasPreview = "true";
        chromaInfo.dataset.hasPreview = "true";
        if (panel && panelButtonRef) positionPanel(panel, panelButtonRef);
      });
      loader.src = url;
    };

    const noneEntry = {
      id: "__none__", modName: t("Base Skin"), thumbnailUrl: "",
      description: t("Disable custom skin mod"), _none: true,
    };

    const visibleMods = [noneEntry, ...mods];

    visibleMods.forEach((mod, index) => {
      const modId = mod._none ? "__none__" : normalizeModId(mod);
      const isSelected = isModSelected(mod, getCurrentSkinContext().skinId);

      const item = document.createElement("li");
      const emberView = document.createElement("div");
      emberView.className = "ember-view";

      const wheelButton = document.createElement("div");
      wheelButton.className = `chroma-skin-button ${isSelected ? "selected" : ""}`;
      wheelButton.title = visibleModName(mod, t("Custom Skin {number}", { number: index + 1 }));

      const contents = document.createElement("div");
      contents.className = "contents";

      const thumbnailUrl = mod.thumbnailUrl ? String(mod.thumbnailUrl).replace("localhost", "127.0.0.1") : "";
      if (thumbnailUrl) {
        contents.style.backgroundImage = `url('${thumbnailUrl}')`;
      } else if (mod._none) {
        contents.style.background = "linear-gradient(135deg,#f0e6d2,#f0e6d2 48%,#be1e37 0,#be1e37 52%,#f0e6d2 0,#f0e6d2)";
      }

      const applySelection = () => {
        if (mod._none) {
          if (selectedModId) sendDeselect(selectedModId);
          closePanel();
          return;
        }
        if (isModSelected(mod, getCurrentSkinContext().skinId)) {
          sendDeselect(selectedModId);
        } else {
          sendSelect(modId, mod);
        }
        closePanel();
      };

      wheelButton.addEventListener("mouseenter", () => {
        setPreviewImage(thumbnailUrl || "", mod._none ? getCurrentSkinContext().skinName : (visibleModName(mod) || getCurrentSkinContext().skinName));
      });

      wheelButton.addEventListener("mouseleave", () => {
        const active = visibleMods.find(e => isModSelected(e, getCurrentSkinContext().skinId));
        const au = active && active.thumbnailUrl ? String(active.thumbnailUrl).replace("localhost", "127.0.0.1") : "";
        const al = active && !active._none ? (visibleModName(active) || getCurrentSkinContext().skinName) : getCurrentSkinContext().skinName;
        setPreviewImage(au, al);
      });

      wheelButton.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); applySelection(); });
      contents.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); applySelection(); });

      wheelButton.appendChild(contents);
      emberView.appendChild(wheelButton);
      item.appendChild(emberView);
      list.appendChild(item);
    });

    const active = visibleMods.find(e => isModSelected(e, getCurrentSkinContext().skinId));
    const au = active && active.thumbnailUrl ? String(active.thumbnailUrl).replace("localhost", "127.0.0.1") : "";
    const al = active && !active._none ? (visibleModName(active) || getCurrentSkinContext().skinName) : getCurrentSkinContext().skinName;
    setPreviewImage(au, al);

    scrollable.appendChild(list);
    modal.appendChild(border);
    modal.appendChild(chromaInfo);
    modal.appendChild(standaloneSkinName);
    modal.appendChild(scrollable);
    flyoutContent.appendChild(modal);
    flyout.appendChild(flyoutContent);
    root.appendChild(flyout);

    return root;
  }

  function positionPanel(panelElement, buttonElement) {
    if (!panelElement || !buttonElement) return;
    const flyout = panelElement.querySelector(".flyout");
    if (!flyout) return;

    const rect = buttonElement.getBoundingClientRect();
    let fr = flyout.getBoundingClientRect();
    if (!fr.width) fr = { width: 305, height: 420 };

    const cx = rect.left + rect.width / 2;
    flyout.style.left = `${Math.max(10, Math.min(cx - fr.width / 2, window.innerWidth - fr.width - 10))}px`;
    flyout.style.top = `${Math.max(10, rect.top - fr.height - 14)}px`;
  }

  function closePanel() {
    const existing = document.getElementById(PANEL_ID);
    if (existing) existing.remove();
    panel = null; panelButtonRef = null; panelSkinItemRef = null;
  }

  function togglePanel(buttonElement, skinItem) {
    if (document.getElementById(PANEL_ID) && panelButtonRef === buttonElement) { closePanel(); return; }
    closePanel();
    if (!modsForCurrentSkin.length) return;

    const built = createPanelForMods(modsForCurrentSkin, buttonElement);
    if (!built) return;

    panel = built; panelButtonRef = buttonElement; panelSkinItemRef = skinItem;
    document.body.appendChild(panel);
    positionPanel(panel, buttonElement);

    setTimeout(() => {
      document.addEventListener("click", function ch(e) {
        if (!panel || !panel.parentNode) { document.removeEventListener("click", ch); return; }
        if (panel.contains(e.target) || buttonElement.contains(e.target)) return;
        closePanel(); document.removeEventListener("click", ch);
      });
    }, 100);
  }

  // --- blocked-skin guards -----------------------------------------------
  //
  // A blocked skin item is a carousel node Ember re-renders whenever the
  // player scrolls or hovers, so binding on the node would mean re-binding
  // every frame and racing the render. Capturing on `document` fires before
  // the carousel's own delegated handler regardless of which node exists,
  // which is the only reliable place to refuse the click.
  //
  // Three events are swallowed, not just `click`: League's carousel can act
  // on `pointerdown`, and keeping the default would still move focus and
  // scroll the list into a skin that cannot be applied.
  const BLOCKED_EVENTS = ["pointerdown", "mousedown", "click"];

  function installBlockedSkinGuards() {
    if (window.__dchBlockedGuardsInstalled) return;
    window.__dchBlockedGuardsInstalled = true;

    const swallow = (event) => {
      const target = event.target;
      if (!target || typeof target.closest !== "function") return;
      if (panel && panel.contains(target)) return;

      const item = target.closest('[data-dch-blocked="1"]');
      if (!item) return;

      // Refuse the interaction but leave the event visible to our own banner,
      // which is how the player learns why nothing happened.
      event.preventDefault();
      event.stopImmediatePropagation();
      const verdict = verdictForItem(item);
      if (verdict) showSkinWarning(item, verdict.skinId, verdict.verdict, true);
    };

    for (const name of BLOCKED_EVENTS) {
      document.addEventListener(name, swallow, true);
    }

    // Keyboard parity: a blocked skin reached with Enter/Space must be
    // refused too, otherwise the guard only covers mouse users.
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
      const active = document.activeElement;
      if (!active || typeof active.closest !== "function") return;
      const item = active.closest('[data-dch-blocked="1"]');
      if (!item) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      const verdict = verdictForItem(item);
      if (verdict) showSkinWarning(item, verdict.skinId, verdict.verdict, true);
    }, true);
  }

  function hideSkinWarning() {
    if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
    if (bannerHost && bannerHost.parentNode) bannerHost.parentNode.removeChild(bannerHost);
    bannerHost = null;
    bannerSkinId = null;
  }

  function showSkinWarning(skinItem, skinId, verdict, isBlocked) {
    hideSkinWarning();
    bannerSkinId = skinId;

    const host = document.createElement("div");
    host.className = "dch-skin-warning-banner";
    host.setAttribute("role", "alert");

    const title = document.createElement("div");
    title.className = "dch-warn-title";
    title.textContent = isBlocked
      ? t("This skin cannot be used")
      : t("This skin will have no skill sounds");

    const body = document.createElement("div");
    body.className = "dch-warn-body";
    const name = document.createElement("strong");
    name.textContent = visibleModName({ modName: skinItem.getAttribute("data-skin-name") || "" }, "")
      || t("Selected skin");
    body.appendChild(name);
    body.appendChild(document.createTextNode(
      isBlocked
        ? " — " + t("LTK Manager refuses this archive (c0000229), so it would never appear in game.")
        : " — " + t("The archive is missing the audio bank it needs, so skill sounds will be silent.")
    ));

    const actions = document.createElement("div");
    actions.className = "dch-warn-actions";

    const close = document.createElement("button");
    close.type = "button";
    close.textContent = isBlocked ? t("Choose another") : t("Use it anyway");
    close.className = "dch-warn-primary";
    close.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      hideSkinWarning();
    });

    actions.appendChild(close);
    host.appendChild(title);
    host.appendChild(body);
    host.appendChild(actions);

    const mount = document.body || document.documentElement;
    mount.appendChild(host);
    bannerHost = host;

    // A warning that never leaves would cover the carousel, so it retires on
    // its own; the player can also dismiss it at once.
    bannerTimer = setTimeout(hideSkinWarning, 9000);
  }

  function ensureButtonOnItem(skinItem) {
    if (!skinItem) return;

    // Some League builds nest the thumbnail wrapper inside the selection
    // item. Keep one D-Changer button per item instead of attaching two buttons to
    // the same skin during a mutation-observer scan.
    if (skinItem.classList.contains("thumbnail-wrapper")) {
      const parentItem = skinItem.closest(".skin-selection-item");
      if (parentItem && Array.from(parentItem.children).some((child) => child.matches(BUTTON_SELECTOR))) {
        return;
      }
    }

    const isCurrent = isCurrentSkinItem(skinItem);
    const matchesState = doesSkinItemMatchSkinState(skinItem);
    let existingButton = skinItem.querySelector(BUTTON_SELECTOR);

    if (!championLocked || !isCurrent || !matchesState) {
      if (existingButton) existingButton.remove();
      return;
    }

    if (!existingButton) {
      existingButton = createFakeButton();
      if (hasOtherPluginButtons(skinItem)) {
        existingButton.style.transform = "translateX(-50%) translateY(-260%)";
      }
      skinItem.appendChild(existingButton);
    }

    updateButtonVisibility(existingButton, modsForCurrentSkin.length > 0);
  }

  // --- skin health -------------------------------------------------------
  //
  // Two problems are readable from the archive before a player queues, and
  // both are reported by the backend preflight:
  //
  //   blocked   LTK Manager refuses the overlay (c0000229). The skin never
  //             appears, so the picker must not let it be chosen. The click
  //             is swallowed in the capture phase, before the carousel's own
  //             Ember handler can act on it.
  //   no_audio  The skin loads but loses its skill sounds. The player may
  //             still want it, so it stays selectable and only warns.
  function requestSkinPreflight() {
    if (!bridge) return;
    const championId = getCurrentSkinContext().championId;
    if (!Number.isFinite(championId) || championId <= 0) return;

    const key = String(championId);
    const now = Date.now();
    // One ChampSelect cannot change which archives are installed, so ask once
    // per champion instead of once per carousel render.
    if (key === preflightForKey && now - preflightRequestedAt < 30000) return;
    preflightForKey = key;
    preflightRequestedAt = now;

    bridge.send({ type: "request-skin-preflight", championId: key });
  }

  function handleSkinPreflight(data) {
    const detail = (data && data.detail) || data;
    if (!detail || detail.type !== "skin-preflight") return;

    const championId = getCurrentSkinContext().championId;
    const replyChampion = Number(detail.championId);
    if (Number.isFinite(replyChampion) && replyChampion > 0 &&
        Number.isFinite(championId) && championId > 0 && replyChampion !== championId) {
      return;
    }

    skinVerdicts.clear();
    const warnings = Array.isArray(detail.warnings) ? detail.warnings : [];
    for (const warning of warnings) {
      const skinId = Number(warning && warning.skinId);
      if (!Number.isFinite(skinId) || skinId <= 0) continue;
      skinVerdicts.set(skinId, {
        kind: String((warning && warning.kind) || "unknown"),
        code: String((warning && warning.code) || ""),
        message: String((warning && warning.message) || ""),
      });
    }

    const blocked = Number(detail.blocked) || 0;
    const noAudio = Number(detail.noAudio) || 0;
    logInfo("Preflight: " + blocked + " blocked, " + noAudio + " without audio");
    skinVerdicts.set("__counts", { blocked: blocked, noAudio: noAudio });
    scanSkinSelection();
  }

  function verdictForItem(skinItem) {
    const skinId = getSkinIdFromItem(skinItem);
    if (!Number.isFinite(skinId) || skinId <= 0) return null;
    const verdict = skinVerdicts.get(skinId);
    if (!verdict || verdict.blocked !== undefined) return null;
    if (verdict.kind !== "blocked" && verdict.kind !== "no_audio") return null;
    return { skinId: skinId, verdict: verdict };
  }

  function markItemVerdict(skinItem) {
    // Badges and the blocked flag are ours: clear them first so a recycled
    // carousel node (or a skin that became healthy) never keeps stale state.
    const stale = skinItem.querySelectorAll(".dch-broken-badge, .dch-noaudio-badge");
    for (const node of stale) node.remove();
    skinItem.removeAttribute("data-dch-blocked");
    skinItem.removeAttribute("aria-disabled");

    const found = verdictForItem(skinItem);
    if (!found) return;

    const verdict = found.verdict;
    const badge = document.createElement("div");
    if (verdict.kind === "blocked") {
      skinItem.setAttribute("data-dch-blocked", "1");
      skinItem.setAttribute("aria-disabled", "true");
      badge.className = "dch-broken-badge";
      badge.textContent = t("Broken");
      badge.title = verdict.message || t("LTK Manager refuses this skin (c0000229).");
    } else {
      badge.className = "dch-noaudio-badge";
      badge.textContent = t("No audio");
      badge.title = verdict.message || t("This skin will lose its skill sounds.");
    }
    skinItem.appendChild(badge);
  }

  function blockBrokenSkinClick(event) {
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    // Our own panel lives inside the item it was opened from; clicks there
    // must keep working even when the item is blocked.
    if (panel && panel.contains(target)) return;
    const item = target.closest('[data-dch-blocked="1"]');
    if (!item) return;

    // Swallow the gesture in the capture phase: Ember's delegated handler on
    // the carousel never sees it, so the client cannot select the skin either.
    event.stopImmediatePropagation();
    event.preventDefault();
    event.stopPropagation();

    const found = verdictForItem(item);
    showWarningBanner(item, found ? found.verdict : null, true);
  }

  function blockBrokenSkinKey(event) {
    if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") return;
    const target = event.target;
    if (!target || typeof target.closest !== "function") return;
    const item = target.closest('[data-dch-blocked="1"]');
    if (!item) return;
    event.stopImmediatePropagation();
    event.preventDefault();
    const found = verdictForItem(item);
    showWarningBanner(item, found ? found.verdict : null, true);
  }

  function closeWarningBanner() {
    if (bannerTimer) { clearTimeout(bannerTimer); bannerTimer = null; }
    if (bannerHost && bannerHost.parentNode) bannerHost.parentNode.removeChild(bannerHost);
    bannerHost = null;
    bannerSkinId = null;
  }

  function showWarningBanner(skinItem, verdict, isBlocked) {
    closeWarningBanner();

    const skinId = getSkinIdFromItem(skinItem);
    bannerSkinId = Number.isFinite(skinId) ? skinId : null;

    const host = document.createElement("div");
    host.className = "dch-skin-warning-banner";
    host.setAttribute("role", "alert");

    const title = document.createElement("div");
    title.className = "dch-warn-title";
    title.textContent = isBlocked
      ? t("Skin unavailable")
      : t("Skill sounds missing");
    host.appendChild(title);

    const body = document.createElement("div");
    body.className = "dch-warn-body";
    const name = document.createElement("strong");
    name.textContent = String(
      (skinMonitorState && skinMonitorState.name) || t("This skin")
    );
    body.appendChild(name);
    body.appendChild(document.createTextNode(" - "));
    body.appendChild(document.createTextNode(
      isBlocked
        ? t("LTK Manager refuses this skin (c0000229), so it will never appear in game. Pick another skin.")
        : (verdict && verdict.message) ||
          t("This skin will load, but its skill sounds will be silent because the archive lost its audio bank.")
    ));
    host.appendChild(body);

    const actions = document.createElement("div");
    actions.className = "dch-warn-actions";

    if (!isBlocked) {
      const keep = document.createElement("button");
      keep.className = "dch-warn-primary";
      keep.textContent = t("Use anyway");
      keep.addEventListener("click", (e) => {
        e.stopPropagation();
        closeWarningBanner();
      });
      actions.appendChild(keep);
    }

    const dismiss = document.createElement("button");
    dismiss.textContent = t("Pick another");
    dismiss.addEventListener("click", (e) => {
      e.stopPropagation();
      closeWarningBanner();
    });
    actions.appendChild(dismiss);
    host.appendChild(actions);

    // Attached to body, not inside the carousel: Ember re-renders the skin
    // list constantly and would tear a nested banner down mid-read.
    document.body.appendChild(host);
    bannerHost = host;

    // Auto-dismiss so a forgotten banner never sits over the client.
    bannerTimer = setTimeout(() => closeWarningBanner(), 12000);
  }

  function scanSkinSelection() {
    const skinItems = document.querySelectorAll(".skin-selection-item, .thumbnail-wrapper");
    // Reflect the preflight verdicts on every item, every scan: the carousel
    // recycles nodes, so a stale badge is a real possibility.
    for (const item of skinItems) markItemVerdict(item);

    if (!skinItems.length) { removeAllButtons(); closePanel(); return; }
    skinItems.forEach(s => ensureButtonOnItem(s));
    if (!(panelSkinItemRef && panelSkinItemRef.isConnected && panelSkinItemRef.querySelector(BUTTON_SELECTOR) && modsForCurrentSkin.length > 0)) closePanel();
  }

  function handleSelectionResult(data) {
    const detail = data?.detail || data;
    if (!detail || detail.type !== "custom-mod-selection-result") return;

    if (pendingSelectionRequest && detail.requestId && detail.requestId !== pendingSelectionRequest.requestId) {
      return;
    }

    const pending = pendingSelectionRequest;
    pendingSelectionRequest = null;

    if (!detail.success) {
      console.warn(`${LOG_PREFIX} Custom skin selection failed: ${detail.error || "unknown error"}`);
      scanSkinSelection();
      return;
    }

    if (detail.operation === "deselect") {
      selectedModId = null;
      selectedModSkinId = null;
    } else if (detail.operation === "select") {
      selectedModId = String(detail.relativePath || detail.modId || pending?.modId || "");
      selectedModSkinId = Number(detail.skinId || getCurrentSkinContext().skinId);
    }

    scanSkinSelection();
  }

  function handleModsResponse(data) {
    const detail = data?.detail || data;
    if (!detail || detail.type !== "skin-mods-response") return;

    const { skinId, championId } = getCurrentSkinContext();
    if (!skinId || !championId) { modsForCurrentSkin = []; scanSkinSelection(); return; }

    const rc = Number(detail.championId);
    if (rc && rc !== championId) return;

    const responseSkinId = Number(detail.skinId);
    if (!responseSkinId || responseSkinId !== skinId) return;

    modsForCurrentSkin = (Array.isArray(detail.mods) ? detail.mods : []).filter((mod) => (
      isModAvailableForSkin(mod, skinId)
    ));
    modsForKey = `${championId}:${skinId}`;

    if (selectedModId && !pendingSelectionRequest) {
      const selectedEntry = modsForCurrentSkin.find((mod) => (
        String(mod?.relativePath || mod?.modName || "") === String(selectedModId)
      ));
      if (!selectedEntry) {
        sendDeselect(selectedModId);
      }
    }

    if (!selectedModId && !pendingSelectionRequest && detail.historicMod) {
      const match = modsForCurrentSkin.find(m => String(m?.relativePath || "").replace(/\\/g, "/") === String(detail.historicMod).replace(/\\/g, "/"));
      if (match) { sendSelect(normalizeModId(match), match); }
    }

    scanSkinSelection();
    if (panel && panel.parentNode && panelButtonRef) closePanel();
  }

  function handleSkinState(event) {
    const detail = event?.detail;
    if (!detail) return;
    resetStaleChromaStateForSkin(detail.skinId);
    skinMonitorState = detail;
    requestModsForCurrentSkin();
    // The picker learns which champion it is looking at only here, so this is
    // the first moment a preflight request can name a champion. Without it the
    // verdicts never arrive and every skin looks fine.
    requestSkinPreflight();
    scanSkinSelection();
    if (panel && panel.parentNode && panelButtonRef) positionPanel(panel, panelButtonRef);
  }

  function observeSkinSelection() {
    const observer = new MutationObserver(() => scanSkinSelection());
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "aria-selected"] });
    setInterval(scanSkinSelection, 500);
  }

  async function init() {
    if (initialized) return;
    initialized = true;

    injectCSS();

    try { bridge = await waitForBridge(); logInfo("Bridge connected"); }
    catch (error) { console.error(`${LOG_PREFIX} Bridge connection failed`, error); }

    if (window.__dchangerSkinState) skinMonitorState = window.__dchangerSkinState;

    if (bridge) {
      bridge.subscribe("skin-mods-response", (data) => handleModsResponse({ detail: data }));
      bridge.subscribe("custom-mod-selection-result", (data) => handleSelectionResult({ detail: data }));
      bridge.subscribe("chroma-state", handleChromaStateUpdate);
      bridge.subscribe("phase-change", handlePhaseChange);
      bridge.subscribe("local-asset-url", (data) => handleLocalAssetUrl(data));
      bridge.subscribe("champion-locked", (data) => {
        championLocked = Boolean(data?.locked);
        if (!championLocked) {
          pythonChromaState = null;
          modsForCurrentSkin = [];
          selectedModId = null;
          selectedModSkinId = null;
          pendingSelectionRequest = null;
          closePanel();
        }
        else { requestModsForCurrentSkin(); }
        scanSkinSelection();
      });
      bridge.subscribe("custom-mod-state", (data) => {
        if (!data) return;
        if (data.active === false) {
          selectedModId = null;
          selectedModSkinId = null;
        } else if (data.relativePath || data.modName) {
          selectedModId = String(data.relativePath || data.modName);
          selectedModSkinId = Number(data.skinId) || Number(getCurrentSkinContext().skinId);
        }
        scanSkinSelection();
      });
      bridge.on("skin-preflight", handleSkinPreflight);
      bridge.onReady(() => {
        bridge.send({ type: "request-local-asset", assetPath: BUTTON_ICON_ASSET_PATH, timestamp: Date.now() });
        requestModsForCurrentSkin();
      });
      bridge.send({ type: "request-local-asset", assetPath: BUTTON_ICON_ASSET_PATH, timestamp: Date.now() });
    }

    window.addEventListener(EVENT_SKIN_STATE, handleSkinState, { passive: true });
    window.addEventListener("resize", () => { if (panel && panel.parentNode && panelButtonRef) positionPanel(panel, panelButtonRef); });
    window.addEventListener("scroll", () => { if (panel && panel.parentNode && panelButtonRef) positionPanel(panel, panelButtonRef); });

    installBlockedSkinGuards();

    observeSkinSelection();
    requestModsForCurrentSkin();
    requestSkinPreflight();
    scanSkinSelection();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
