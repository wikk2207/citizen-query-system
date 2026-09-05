/**
 * SAAMS Voice UI Agent — LISTEN → UNDERSTAND → ACT → CONFIRM → NEXT STEP
 * Fixes applied:
 *   - normalizeEmail: adds "at the rate" → @, robust dot/at replacements
 *   - extractEmail: validates @ presence before returning
 *   - normalizeName: preserves casing, trims spaces
 *   - Role-based redirect after OTP verify (whitelist for mentor email)
 *   - Multi-user session safety: state reset helpers
 */

(function () {
  // Unified global state for voice assistant
  window.SAAMSVoiceState = window.SAAMSVoiceState || {
    voiceEnabled: localStorage.getItem('saams_voice') !== 'off',
    isSpeaking: false,
    isListening: false,
    commandHandled: false,
    shouldListen: false,
    currentStep: null,
  };
  const S = window.SAAMSVoiceState;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  let paused = false;
  let autoActionsEnabled = true;

  // ── Mentor whitelist ──────────────────────────────────────────────────
  // Strict whitelist: ONLY this email gets the mentor dashboard.
  // Server-side role checks are authoritative; do not embed privileged emails in client code.
  const MENTOR_EMAIL_WHITELIST = [];

  function isMentorEmail(email) {
    if (!email) return false;
    return MENTOR_EMAIL_WHITELIST.includes(email.toLowerCase().trim());
  }

  function urls() {
    return window.SAAMS?.urls || {};
  }

  // ── Speak helpers ─────────────────────────────────────────────────────
  async function speak(text, onDone, resultOnly = false) {
    if (paused || !text || !S.voiceEnabled) {
      if (onDone) onDone();
      return;
    }
    if (S.isSpeaking) return;
    S.isSpeaking = true;
    try { window.SAAMSVoice?.stopListening?.(); } catch (_) {}
    window.speechSynthesis.cancel();
    const utterance = new window.SpeechSynthesisUtterance(text);
    utterance.lang = 'en-US';
    utterance.onend = () => {
      S.isSpeaking = false;
      if (onDone) onDone();
    };
    utterance.onerror = () => {
      S.isSpeaking = false;
      if (onDone) onDone();
    };
    window.speechSynthesis.speak(utterance);
  }

  async function speakListen(text, onDone) {
    if (paused || !S.voiceEnabled) {
      if (onDone) onDone();
      return;
    }
    await speak(text);
    if (typeof onDone === 'function') setTimeout(onDone, 120);
  }

  // ── DOM wait helpers ──────────────────────────────────────────────────
  function waitFor(selector, maxMs = 3000, intervalMs = 100) {
    return new Promise((resolve) => {
      const start = Date.now();
      const tick = () => {
        const el = typeof selector === 'string' ? document.querySelector(selector) : selector();
        if (el) return resolve(el);
        if (Date.now() - start >= maxMs) return resolve(null);
        setTimeout(tick, intervalMs);
      };
      tick();
    });
  }

  async function runWithRetry(actionFn, selector, retries = 4) {
    for (let i = 0; i < retries; i++) {
      if (selector) await waitFor(selector, 2000, 80);
      if (actionFn()) return true;
      if (i < retries - 1) {
        speak('Waiting for page to load. Retrying…');
        await sleep(1200);
      }
    }
    speak('Could not complete that action. Please use the form manually.');
    return false;
  }

  // ── Field fill ────────────────────────────────────────────────────────
  function fieldEl(name) {
    return document.querySelector(
      `[data-voice-field="${name}"], [name="${name}"], #${name}`
    );
  }

  function fillField(name, value) {
    if (!value || !autoActionsEnabled || !S.voiceEnabled) return false;
    if (/password|confirm_password/i.test(name)) return false;
    const el = fieldEl(name);
    if (!el || el.type === 'password') return false;
    if (el.tagName === 'SELECT') {
      const v = String(value).toLowerCase();
      const opt = [...el.options].find(
        (o) => o.value.toLowerCase() === v || o.text.toLowerCase().includes(v)
      );
      el.value = opt ? opt.value : value;
      el.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      el.value = value;
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    el.classList.add('voice-filled');
    el.classList.add('voice-field-active');
    return true;
  }

  function clickSelectors(list) {
    if (!autoActionsEnabled) return false;
    const sels = Array.isArray(list) ? list : [list];
    for (const sel of sels) {
      const btn = document.querySelector(sel);
      if (btn) { btn.click(); return true; }
    }
    return false;
  }

  // ── Button selector map ───────────────────────────────────────────────
  const CLICK = {
    sendOtp:  ['#sendOtpBtn', '[data-voice-action="send-otp"]', 'form button[type="submit"]'],
    verifyOtp: ['#verifyOtpBtn', '[data-voice-action="verify-otp"]', 'form button[type="submit"]'],
    register: ['#registerBtn', '[data-voice-action="register"]', '#registerForm button[type="submit"]', 'form button[type="submit"]'],
  };

  // ── Navigation ────────────────────────────────────────────────────────
  function navigate(url, fast = true) {
    if (!url) return;
    if (fast && window.SAAMSSpa?.navigate) {
      window.SAAMSSpa.navigate(url, { delay: 280 });
    } else {
      window.location.href = url;
    }
  }

  // ── Role-based redirect ───────────────────────────────────────────────
  /**
   * Called after OTP is verified. Redirects to mentor or student dashboard
   * based on strict whitelist. ALL other emails go to student dashboard.
   */
  function redirectAfterLogin(email) {
    const u = urls();
    if (isMentorEmail(email)) {
      const dest = u.mentorDashboard || '/mentor/dashboard';
      speak('Welcome, mentor. Opening mentor dashboard.');
      setTimeout(() => navigate(dest, false), 800);
    } else {
      const dest = u.studentDashboard || '/student/dashboard';
      speak('Login successful. Opening student dashboard.');
      setTimeout(() => navigate(dest, false), 800);
    }
  }

  // ── Email normalization (FIXED) ───────────────────────────────────────
  /**
   * Normalizes a spoken email string:
   *   "binary AI zero zero ten at the rate gmail dot com"
   *
   * Order matters: "at the rate" must be replaced before standalone "at".
   */
  function normalizeEmail(spoken) {
    // Spoken digit words → digits
    const withDigits = spoken
      .toLowerCase()
      .replace(/\bzero\b/g, '0')
      .replace(/\bone\b/g, '1')
      .replace(/\btwo\b/g, '2')
      .replace(/\bthree\b/g, '3')
      .replace(/\bfour\b/g, '4')
      .replace(/\bfive\b/g, '5')
      .replace(/\bsix\b/g, '6')
      .replace(/\bseven\b/g, '7')
      .replace(/\beight\b/g, '8')
      .replace(/\bnine\b/g, '9');

    return withDigits
      .replace(/at the rate/g, '@')   // "at the rate" → @  (must come before \bat\b)
      .replace(/\bat\b/g, '@')         // standalone "at" → @
      .replace(/\bdot\b/g, '.')        // "dot" → .
      .replace(/\s+/g, '');            // remove all spaces
  }

  /**
   * Tries to extract a valid email from a spoken string.
   * Returns null if no valid email (containing @ and .) is found.
   */
  function extractEmail(text) {
    // Direct regex match (if user speaks the actual email characters)
    const m = text.match(/[\w.+-]+@[\w.-]+\.\w+/i);
    if (m) return m[0].toLowerCase();

    // Spoken-word style ("at", "dot", "at the rate")
    if (/at the rate|\bat\b|\bdot\b/i.test(text)) {
      const normalized = normalizeEmail(text);
      // Only return if it looks like a valid email
      if (normalized.includes('@') && normalized.includes('.')) return normalized;
    }
    return null;
  }

  // ── Name normalization (NEW — preserves casing) ───────────────────────
  /**
   * Normalizes a spoken name:
   *   - Trims leading/trailing whitespace
   *   - Collapses multiple spaces
   *   - Preserves original capitalization (no toLowerCase)
   *
   * "  Ashu  Sharma " → "Ashu Sharma"
   */
  function normalizeName(spoken) {
    return (spoken || '').trim().replace(/\s+/g, ' ');
  }

  // ── OTP / phone normalization ─────────────────────────────────────────
  function extractOtp(text) {
    // Digits only
    const digits = text.replace(/\D/g, '');
    if (digits.length >= 4) return digits.slice(0, 6);
    const m = text.match(/\b(\d{4,8})\b/);
    return m ? m[1].slice(0, 6) : '';
  }

  function normalizeMobile(spoken) {
    return spoken.replace(/\D/g, '').slice(-10);
  }

  function parseYear(spoken) {
    const t = spoken.toLowerCase();
    if (/first|1st|one/.test(t))   return '1';
    if (/second|2nd|two/.test(t))  return '2';
    if (/third|3rd|three/.test(t)) return '3';
    if (/fourth|4th|four/.test(t)) return '4';
    return '';
  }

  // ── Session reset (multi-user safety) ────────────────────────────────
  /**
   * Resets all voice-flow state refs for a clean new user session.
   * Call on logout, page change, or beginning a new login/register.
   */
  function resetSession() {
    // Reset VoiceMemory session storage
    sessionStorage.removeItem('saams_guide_active');
    sessionStorage.removeItem('saams_guide_step');
    sessionStorage.removeItem('saams_guide_flow');
    sessionStorage.removeItem('saams_welcome_done');
    sessionStorage.removeItem('saams_welcomed_back');
    sessionStorage.removeItem('saams_voice_memory_v1');

    // Reset VoiceMemory object
    if (window.VoiceMemory) {
      window.VoiceMemory.currentStep  = null;
      window.VoiceMemory.currentFlow  = null;
      window.VoiceMemory.otpSent      = false;
      window.VoiceMemory.verified     = false;
      window.VoiceMemory.welcomeDone  = false;
      window.VoiceMemory.completedSteps = new Set();
      window.VoiceMemory.spokenTexts    = new Set();
      window.VoiceMemory.lastSpoken     = null;
    }

    // Stop any active speech / recognition
    window.speechSynthesis?.cancel();
    if (window.SAAMSVoice) {
      try { window.SAAMSVoice.stopListening?.(); } catch (_) {}
    }
  }

  // ── Interrupt handler ─────────────────────────────────────────────────
  function handleInterrupt(raw) {
    const t = raw.toLowerCase().trim();
    if (/turn off guide|stop voice guide|disable assistant/.test(t)) {
      window.VoiceMemoryAPI?.disableGuide?.();
      return true;
    }
    if (/turn on guide|enable assistant|enable guide/.test(t)) {
      window.VoiceMemoryAPI?.enableGuide?.();
      return true;
    }
    if (/stop voice|pause assistant|turn off voice|disable voice|stop listening|manual mode/.test(t)) {
      paused = true;
      autoActionsEnabled = false;
      window.speechSynthesis?.cancel();
      window.SAAMSVoice?.stopListening?.();
      window.SAAMSGuide?.stop?.();
      localStorage.setItem('saams_voice', 'off');
      speak('Voice assistant paused.', () => {}, true);
      return true;
    }
    if (/resume assistant|turn voice on|enable voice/.test(t)) {
      paused = false;
      autoActionsEnabled = true;
      localStorage.setItem('saams_voice', 'on');
      speak('Voice assistant resumed.');
      return true;
    }
    return false;
  }

  // ── Command stacks ────────────────────────────────────────────────────
  function splitStack(raw) {
    const parts = raw
      .split(/\s*(?:then|and then|->|→|,)\s*/i)
      .map((s) => s.trim())
      .filter((s) => s.length > 1);
    return parts.length >= 2 ? parts : null;
  }

  async function executeStack(parts) {
    for (const part of parts) {
      if (paused) break;
      await processPhrase(part);
      await sleep(350);
    }
  }

  async function executeStackFromRaw(raw) {
    const parts = splitStack(raw);
    if (!parts) return false;
    await executeStack(parts);
    return true;
  }

  async function processPhrase(raw) {
    const t = raw.toLowerCase().trim();
    const u = urls();

    if (/^login$|log in|sign in/.test(t)) { window.SAAMSGuide?.beginLogin?.(); return; }
    if (/^register$|sign up|create account/.test(t)) { window.SAAMSGuide?.beginRegister?.(); return; }
    if (/send otp|click send/.test(t)) {
      await runWithRetry(() => clickSelectors(CLICK.sendOtp), CLICK.sendOtp[0]);
      speak('OTP has been sent to your email.');
      return;
    }
    if (/verify|verify otp/.test(t)) {
      await runWithRetry(() => clickSelectors(CLICK.verifyOtp), CLICK.verifyOtp[0]);
      speak('Verifying.');
      return;
    }
    if (/dashboard|open dashboard/.test(t)) {
      navigate(u.studentDashboard || u.mentorDashboard);
      return;
    }
    const email = extractEmail(raw);
    if (email) {
      await runWithRetry(() => fillField('email', email), '[data-voice-field="email"]');
      if (/send otp|login/.test(t)) {
        await runWithRetry(() => clickSelectors(CLICK.sendOtp), CLICK.sendOtp[0]);
        speak('OTP has been sent to your email.');
      }
      return;
    }
    const otp = extractOtp(raw);
    if (otp) {
      await runWithRetry(() => fillField('code', otp), '[data-voice-field="code"]');
      await runWithRetry(() => clickSelectors(CLICK.verifyOtp), CLICK.verifyOtp[0]);
      speak('Verification successful. Redirecting to dashboard.');
    }
  }

  async function tryCompound(raw) {
    const t = raw.toLowerCase();
    if (!/login|send otp|verify|register|dashboard/.test(t)) return false;

    const email = extractEmail(raw);
    const otp   = extractOtp(raw);

    if (email && /send otp|login/.test(t)) {
      if (!window.location.pathname.includes('login')) {
        window.SAAMSGuide?.beginLogin?.();
        await sleep(700);
      }
      sessionStorage.setItem('saams_guide_active', '1');
      sessionStorage.setItem('saams_guide_flow', 'login');
      await runWithRetry(() => fillField('email', email), '[data-voice-field="email"]');
      await runWithRetry(() => clickSelectors(CLICK.sendOtp), CLICK.sendOtp[0]);
      window.SAAMSGuide?.setStep?.('login_otp');
      speak('OTP has been sent to your email.');
      return true;
    }

    if (otp && /verify/.test(t)) {
      await runWithRetry(() => fillField('code', otp), '[data-voice-field="code"]');
      await runWithRetry(() => clickSelectors(CLICK.verifyOtp), CLICK.verifyOtp[0]);
      speak('Verification successful. Redirecting to dashboard.');
      return true;
    }

    if (/login.*send otp.*verify|send otp.*verify.*dashboard/.test(t)) {
      await executeStackFromRaw(raw.replace(/\band\b/gi, ' then '));
      return true;
    }

    return false;
  }

  // ── Public API ────────────────────────────────────────────────────────
  window.SAAMSAgent = {
    paused: () => paused,
    pause() {
      paused = true;  autoActionsEnabled = false;
      S.voiceEnabled = false;
      S.isSpeaking = false;
      S.isListening = false;
      S.commandHandled = false;
      S.shouldListen = false;
      S.currentStep = null;
      window.speechSynthesis.cancel();
      try { window.SAAMSVoice?.stopListening?.(); } catch (_) {}
    },
    resume() {
      paused = false; autoActionsEnabled = true;
      S.voiceEnabled = true;
      S.isSpeaking = false;
      S.isListening = false;
      S.commandHandled = false;
      S.shouldListen = false;
      S.currentStep = null;
    },
    waitFor,
    sleep,
    runWithRetry,
    fillField,
    clickSelectors,
    CLICK,
    navigate,
    speak,
    speakListen,
    // Normalization utilities
    normalizeEmail,
    normalizeName,
    extractEmail,
    extractOtp,
    normalizeMobile,
    parseYear,
    // Role helpers
    isMentorEmail,
    redirectAfterLogin,
    // Session safety
    resetSession,
    // Command processing
    handleInterrupt,
    splitStack,
    executeStackFromRaw,
    tryCompound,
    processPhrase,
  };
})();
