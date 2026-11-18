// Fixed REPL program. The URL is JSON data, never user-provided JavaScript.
const target = __SIPHER_URL_JSON__;
let tab;
let result;
try {
  tab = await openTab(target);
  const collected = new Map();
  // Invocation-local bookkeeping, passed as data rather than stored on the page.
  let clicked = {notice: false, panel: false, comments: false};
  let latest;
  for (let attempt = 0; attempt < 10; attempt++) {
    const previousClicks = Object.values(clicked).filter(Boolean).length;
    const observed = await tab.evaluate(({expectedPath, clicked, previousClicks, attempt}) => {
      const read = (allowNavigation = true) => {
      // Read location before any content. Reject redirects outside a post page.
      const here = new URL(location.href);
      if (here.protocol !== "https:" || !["www.tiktok.com", "tiktok.com"].includes(here.host)) {
        return {status: "redirect_rejected", url: location.href};
      }
      if (/^\/(login|passport)(\/|$)/i.test(here.pathname)) return {status: "auth_required", signal_source: "login_redirect"};
      if (/^\/(captcha|verify)(\/|$)/i.test(here.pathname)) return {status: "captcha", signal_source: "captcha_redirect"};
      if (/^\/checkpoint(\/|$)/i.test(here.pathname)) return {status: "account_warning", signal_source: "checkpoint_redirect"};
      if (!/^\/@[A-Za-z0-9_.-]+\/(video|photo)\/[0-9]+$/.test(here.pathname)) {
        return {status: "redirect_rejected", url: location.href};
      }
      if (here.pathname !== expectedPath) return {status: "redirect_rejected"};
      const visible = el => !!el && (el.getClientRects().length > 0);
      const text = el => el ? (el.innerText || "").trim() : "";
      // Account signals are read only from dedicated system UI, never post text.
      const system = [...document.querySelectorAll('[data-e2e="system-error"], [data-e2e="error-message"], [data-e2e="captcha-container"]')]
        .filter(visible).map(text).join("\n");
      const warnings = system;
      if (/captcha|verify (that )?you are human|보안 확인/i.test(warnings)) return {status: "captcha", signal_source: "system_ui"};
      if ([...document.querySelectorAll('iframe[src*="captcha"]')].some(visible)) return {status: "captcha", signal_source: "captcha_iframe"};
      if (/feedback_required|\b429\b|checkpoint|rate.?limit|too many requests|access denied|\bblocked\b|차단|속도 제한/i.test(warnings) ||
          /\bspam\b/i.test(system)) return {status: "account_warning", signal_source: "system_ui"};
      if (/log in to continue|login required|로그인.{0,10}필요/i.test(warnings)) return {status: "auth_required", signal_source: "system_ui"};
      if ([...document.querySelectorAll('input[type="password"], [data-e2e="login-modal"]')].some(visible)) return {status: "auth_required", signal_source: "login_ui"};
      if (/video (is )?(unavailable|not available)|couldn't find this video|게시물을 찾을 수|삭제된 게시물/i.test(warnings)) {
        return {status: "not_found"};
      }
      // Anchors observed on the live post page (LIVE-DOM-OBSERVATION-20261113).
      const body = document.querySelector('[data-e2e="video-desc"]');
      const area = document.querySelector('[class*="DivCommentListContainer"]');
      // One wrapper per top-level comment; only its level-1 anchors are read.
      const nodes = visible(area) ? [...area.querySelectorAll('[class*="DivCommentObjectWrapper"]')].filter(visible) : [];
      const comments = nodes.slice(0, 20).map(node => {
        const content = node.querySelector('[data-e2e="comment-level-1"]');
        const author = node.querySelector('[data-e2e="comment-username-1"] a[href^="/@"]');
        return {id: node.getAttribute("data-comment-id"), author: text(author) || null, text: text(content)};
      });
      // Never promote the second readable row into a claimed first comment.
      if (comments.some(c => !c.text)) return {status: "comments_unavailable"};
      const emptyNode = area && area.querySelector('[data-e2e="comment-empty"], [data-e2e="comment-empty-state"]');
      const empty = visible(area) && visible(emptyNode) && /^(no comments yet|be the first to comment|아직 댓글이 없습니다[.!]?|첫 댓글을 남겨보세요[.!]?)$/i.test(text(emptyNode));
      if (allowNavigation && !visible(area) && !comments.length && !empty) {
        // Unobserved live DOM: use roles and exact visible/accessibility names.
        // Existing comment-list selectors above remain the content authority.
        const safe = node => visible(node) && !node.disabled && node.getAttribute("aria-disabled") !== "true" &&
          !node.closest('a, [role="link"], input, textarea, video, [contenteditable]');
        const named = (node, pattern) => [node.getAttribute("aria-label"), node.getAttribute("title"), text(node)]
          .some(value => pattern.test((value || "").trim()));
        const controls = root => [...root.querySelectorAll('button, [role="button"], [role="tab"]')].filter(safe);
        const guide = [...document.querySelectorAll('[role="dialog"], [role="alertdialog"]')].filter(visible)
          .find(node => /피드를 둘러보|(?:scroll|arrow keys).*(?:feed|browse)|(?:browse|explore).*feed/i.test(text(node)));
        const commentName = /^(?:댓글|comments)(?:\s*\(?[\d.,KMB천만]+\s*개?\)?)?$/i;
        let kind;
        let control;
        if (guide) {
          kind = "notice";
          control = controls(guide).find(node => named(node, /^(?:확인|닫기|OK|Got it|Close)$/i));
        } else {
          const candidates = controls(document).filter(node => named(node, commentName));
          const commentTab = candidates.find(node => node.getAttribute("role") === "tab" || node.closest('[role="tablist"]'));
          kind = commentTab ? "comments" : "panel";
          control = commentTab || candidates.find(node => node.getAttribute("role") !== "tab");
        }
        if (control && !clicked[kind]) {
          clicked[kind] = true;
          control.click();
          // Recheck synchronously exposed warnings; asynchronous changes are read
          // on the next bounded iteration before any further navigation/scroll.
          return read(false);
        }
      }
      // The post author is the requested post's handle (expectedPath, already equal
      // to the current path), reported only when an observed avatar link points
      // exactly to /@<that handle>. Document order never chooses the author.
      const owner = /^\/@([A-Za-z0-9_.-]+)\//.exec(expectedPath)[1];
      const ownerAvatar = [...document.querySelectorAll('[data-e2e="video-author-avatar"]')]
        .some(avatar => avatar.getAttribute("href") === "/@" + owner);
      return {status: "ok", url: location.href, body_found: !!body, body_text: text(body),
              author: ownerAvatar ? owner : null,
              comments, comments_state: comments.length ? "visible" : empty ? "empty" : "unavailable"};
      };
      const snapshot = read(attempt < 9);
      // Keep scrolling in the same evaluation: at most ten page evaluations in
      // total, with no scroll between a navigation click and the next guard.
      if (snapshot.status === "ok" && Object.values(clicked).filter(Boolean).length === previousClicks &&
          attempt < 5 && !(snapshot.body_found && (snapshot.comments.length >= 20 || snapshot.comments_state === "empty"))) {
        const area = document.querySelector('[class*="DivCommentListContainer"]');
        if (area) area.scrollBy(0, 500);
      }
      return [snapshot, clicked];
    }, {expectedPath: new URL(target).pathname, clicked, previousClicks, attempt});
    [latest, clicked] = observed;
    if (latest.status !== "ok") { result = latest; break; }
    const actual = new URL(latest.url);
    if (actual.pathname !== new URL(target).pathname) { result = {status: "redirect_rejected"}; break; }
    if (Object.values(clicked).filter(Boolean).length > previousClicks) {
      if (attempt < 9) await new Promise(resolve => setTimeout(resolve, 1000));
      continue;
    }
    for (const c of latest.comments) {
      const key = c.id || JSON.stringify([c.author, c.text]);
      if (!collected.has(key) && collected.size < 20) collected.set(key, c);
    }
    if (latest.body_found && (collected.size >= 20 || latest.comments_state === "empty" || attempt === 9)) {
      result = {...latest, comments: [...collected.values()],
                comments_state: collected.size ? "visible" : latest.comments_state};
      break;
    }
    if (attempt < 9) await new Promise(resolve => setTimeout(resolve, 1000));
  }
  if (!result || result.comments_state === "unavailable") result = {status: "comments_unavailable"};
} finally {
  // Close only the tab created by this invocation, even after a warning.
  if (tab) {
    try { await closeTab(tab); }
    catch {
      if (!result || result.status === "ok") result = {status: "cleanup_failed"};
      result.cleanup_failed = true;
    }
  }
}
console.log("SIPHER_TIKTOK_ASIDE:" + JSON.stringify(result));
