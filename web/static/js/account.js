/* Account widget: header pill / chip, modal with live username check, logout.
 *
 * Wires three DOM patches:
 *   1. The Login pill (when logged out) opens a native <dialog> with a 2-field
 *      form. As the user types the username, a debounced POST /api/account/check
 *      flips the submit-button label between "Log in" / "Create account" and
 *      renders a hint row below the field. Submit → POST /api/account/auth →
 *      stash a sessionStorage flash → window.location.reload() so the server
 *      re-renders the header chip on the next page.
 *   2. The chip (when logged in) opens a small popover with one Log out item.
 *   3. On every page load, look for a sessionStorage flash and pop a 2.5 s
 *      toast (used to surface "Welcome back!" / "Account created!" after the
 *      post-auth reload).
 *
 * No framework. Native <dialog> handles focus trap, ESC, and modal backdrop
 * click for free. */

(function () {
  'use strict';

  function t(key, vars) {
    return (window.tfmt ? window.tfmt(key, vars) : key);
  }

  // ─── Toast (consumed on page load if a flash is set) ────────────────────
  function showToast(message) {
    var el = document.createElement('div');
    el.className = 'account-toast';
    el.setAttribute('role', 'status');
    el.textContent = message;
    document.body.appendChild(el);
    setTimeout(function () {
      el.style.transition = 'opacity 0.25s ease';
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 260);
    }, 2500);
  }

  function consumeFlash() {
    try {
      var flash = sessionStorage.getItem('account.flash');
      var name = sessionStorage.getItem('account.flash.name') || '';
      if (!flash) return;
      sessionStorage.removeItem('account.flash');
      sessionStorage.removeItem('account.flash.name');
      if (flash === 'created')      showToast(t('account.toast.created', { name: name }));
      else if (flash === 'welcome_back') showToast(t('account.toast.welcome_back', { name: name }));
    } catch (_) { /* sessionStorage may be unavailable in some browsers */ }
  }

  // ─── Chip popover (logged-in state) ─────────────────────────────────────
  function wirePopover() {
    var chip = document.querySelector('.site-header-account-chip');
    var pop  = document.querySelector('.site-header-account-popover');
    if (!chip || !pop) return;

    function open()  { pop.removeAttribute('hidden'); }
    function close() { pop.setAttribute('hidden', ''); }

    chip.addEventListener('click', function (e) {
      e.stopPropagation();
      if (pop.hasAttribute('hidden')) open(); else close();
    });

    document.addEventListener('click', function (e) {
      if (pop.hasAttribute('hidden')) return;
      if (pop.contains(e.target) || chip.contains(e.target)) return;
      close();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !pop.hasAttribute('hidden')) close();
    });

    var logoutBtn = pop.querySelector('[data-account-logout]');
    if (logoutBtn) {
      logoutBtn.addEventListener('click', function () {
        fetch('/api/account/logout', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Accept': 'application/json' },
        }).then(function () { window.location.reload(); })
          .catch(function () { window.location.reload(); });
      });
    }
  }

  // ─── Login pill → modal ─────────────────────────────────────────────────
  function wireDialog() {
    var pill   = document.querySelector('[data-account-open]');
    var dialog = document.getElementById('account-dialog');
    if (!dialog) return;
    var form   = dialog.querySelector('form');
    var userIn = dialog.querySelector('[name=username]');
    var passIn = dialog.querySelector('[name=password]');
    var submit = dialog.querySelector('[type=submit]');
    var errBox = dialog.querySelector('[data-account-err]');
    var hintRow = dialog.querySelector('[data-account-hint]');
    var passToggle = dialog.querySelector('[data-account-password-toggle]');
    var closeBtn = dialog.querySelector('[data-account-close]');
    var roleSet = dialog.querySelector('[data-account-role]');
    var roleStudentRadio = dialog.querySelector('#account-role-student');
    var checksList = dialog.querySelector('[data-account-pw-checks]');
    var checkItems = checksList ? checksList.querySelectorAll('[data-check]') : [];

    if (!form || !userIn || !passIn || !submit) return;

    if (pill) {
      pill.addEventListener('click', function () {
        resetDialog();
        try { dialog.showModal(); } catch (_) { dialog.setAttribute('open', ''); }
        setTimeout(function () { userIn.focus(); }, 0);
      });
    }
    if (closeBtn) {
      closeBtn.addEventListener('click', function () { dialog.close(); });
    }

    if (passToggle) {
      passToggle.addEventListener('click', function () {
        var isText = passIn.type === 'text';
        passIn.type = isText ? 'password' : 'text';
        passToggle.setAttribute('aria-pressed', String(!isText));
      });
    }

    // ─── State for live check ─────────────────────────────────────────────
    // mode: 'neutral' | 'login' | 'create'
    var mode = 'neutral';
    var debounceTimer = null;
    var inflight = null;       // AbortController
    var lastChecked = null;    // last username string we got an answer for

    function setMode(next, name) {
      mode = next;
      if (next === 'login') {
        submit.textContent = t('account.dialog.submit_login');
      } else if (next === 'create') {
        submit.textContent = t('account.dialog.submit_create');
      } else {
        submit.textContent = t('account.dialog.submit_neutral');
      }
      // Role radio + password requirements are visible by default (so a
      // fresh visitor opening the modal sees them immediately). Both hide
      // on the login path — returning users get a clean two-field form, and
      // their (possibly pre-existing weak) password isn't held to the new
      // signup rules.
      if (roleSet) {
        if (next === 'login') roleSet.setAttribute('hidden', '');
        else roleSet.removeAttribute('hidden');
      }
      if (checksList) {
        if (next === 'login') checksList.setAttribute('hidden', '');
        else checksList.removeAttribute('hidden');
      }
      updateSubmitEnabled();
      // Hint row
      hintRow.innerHTML = '';
      hintRow.classList.remove('account-hint--found', 'account-hint--new');
      if (next === 'login' && name) {
        hintRow.classList.add('account-hint--found');
        hintRow.innerHTML =
          '<span class="account-hint-icon" aria-hidden="true">✓</span>' +
          '<span></span>';
        hintRow.lastChild.textContent = t('account.hint.found', { name: name });
      } else if (next === 'create' && name) {
        hintRow.classList.add('account-hint--new');
        hintRow.innerHTML =
          '<span class="account-hint-icon" aria-hidden="true">+</span>' +
          '<span></span>';
        hintRow.lastChild.textContent = t('account.hint.new', { name: name });
      }
    }

    // Mirror of the server-side rules in web/user_auth.validate_signup_password.
    // Kept here as DOM-only checks so the UI reflects them instantly.
    function passwordChecks(pw) {
      return {
        length: pw.length >= 8,
        letter: /\p{L}/u.test(pw),
        digit:  /\p{N}/u.test(pw),
      };
    }

    function renderPasswordChecks() {
      var pw = passIn.value;
      var checks = passwordChecks(pw);
      for (var i = 0; i < checkItems.length; i++) {
        var item = checkItems[i];
        var which = item.getAttribute('data-check');
        if (checks[which]) item.classList.add('met');
        else item.classList.remove('met');
      }
      return checks.length && checks.letter && checks.digit;
    }

    function updateSubmitEnabled() {
      var basicsOk = userIn.value.trim().length && passIn.value.length;
      // In login mode, any-length-non-empty password is accepted (the user's
      // existing password may pre-date the strength rules). In create/neutral
      // mode, require the live checks to all pass — matches the server-side
      // signup validator.
      var pwOk = (mode === 'login') ? true : renderPasswordChecks();
      submit.disabled = !(basicsOk && pwOk);
    }

    function clearError() {
      errBox.textContent = '';
      errBox.hidden = true;
    }

    function showError(msg) {
      errBox.textContent = msg;
      errBox.hidden = false;
    }

    function resetDialog() {
      userIn.value = '';
      passIn.value = '';
      passIn.type = 'password';
      if (passToggle) passToggle.setAttribute('aria-pressed', 'false');
      if (roleStudentRadio) roleStudentRadio.checked = true;
      clearError();
      setMode('neutral', '');
      updateSubmitEnabled();
      lastChecked = null;
      if (inflight) { try { inflight.abort(); } catch (_) {} inflight = null; }
      if (debounceTimer) { clearTimeout(debounceTimer); debounceTimer = null; }
    }

    function runCheck() {
      var name = userIn.value.trim();
      if (!name) { setMode('neutral', ''); lastChecked = null; return; }
      if (name === lastChecked) return;
      if (inflight) { try { inflight.abort(); } catch (_) {} }
      inflight = ('AbortController' in window) ? new AbortController() : null;
      var opts = {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ username: name }),
      };
      if (inflight) opts.signal = inflight.signal;
      fetch('/api/account/check', opts)
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (body) {
          if (!body) return;
          lastChecked = name;
          setMode(body.exists ? 'login' : 'create', name);
        })
        .catch(function () { /* silent: leave mode/hint as-is */ });
    }

    userIn.addEventListener('input', function () {
      updateSubmitEnabled();
      // Reset hint immediately so a stale "Welcome back" doesn't linger.
      if (mode !== 'neutral') setMode('neutral', '');
      if (debounceTimer) clearTimeout(debounceTimer);
      debounceTimer = setTimeout(runCheck, 400);
    });
    passIn.addEventListener('input', updateSubmitEnabled);

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      clearError();
      submit.disabled = true;
      var name = userIn.value.trim();
      var pw = passIn.value;
      var payload = { username: name, password: pw };
      // Send the user's role selection whenever the role fieldset is visible
      // (i.e. mode is 'neutral' or 'create'). On login mode the server
      // ignores it anyway, but more importantly: if the user submits before
      // the live-check has fired (mode still 'neutral'), the server may
      // end up taking the create path — we don't want their Teacher
      // selection silently dropped to the 'student' default.
      if (mode !== 'login') {
        var roleSel = dialog.querySelector('input[name=role]:checked');
        if (roleSel && roleSel.value) payload.role = roleSel.value;
      }
      fetch('/api/account/auth', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify(payload),
      })
        .then(function (r) {
          return r.json().then(function (body) { return { ok: r.ok, status: r.status, body: body }; });
        })
        .then(function (res) {
          if (res.ok && res.body && res.body.ok) {
            try {
              sessionStorage.setItem('account.flash', res.body.created ? 'created' : 'welcome_back');
              sessionStorage.setItem('account.flash.name', res.body.username || name);
            } catch (_) {}
            window.location.reload();
            return;
          }
          if (res.status === 429) {
            showError(t('account.err.rate_limit'));
          } else if (res.body && res.body.detail) {
            showError(t(res.body.detail));
          } else {
            showError(t('account.err.network'));
          }
          updateSubmitEnabled();
        })
        .catch(function () {
          showError(t('account.err.network'));
          updateSubmitEnabled();
        });
    });

    // Initial state.
    resetDialog();
  }

  // ─── Boot ───────────────────────────────────────────────────────────────
  function init() {
    consumeFlash();
    wirePopover();
    wireDialog();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
}());
