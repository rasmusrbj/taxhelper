(function () {
  const storageKey = "taxhelper-language";
  const root = document.body;
  const languageButtons = document.querySelectorAll("[data-set-lang]");

  function setLanguage(language) {
    const normalized = language === "da" ? "da" : "en";
    root.dataset.lang = normalized;
    document.documentElement.lang = normalized === "da" ? "da" : "en";
    languageButtons.forEach((button) => {
      const isActive = button.dataset.setLang === normalized;
      button.setAttribute("aria-pressed", String(isActive));
    });
    try {
      window.localStorage.setItem(storageKey, normalized);
    } catch {
      // Ignore storage failures in private browsing or locked-down clients.
    }
  }

  function preferredLanguage() {
    try {
      const saved = window.localStorage.getItem(storageKey);
      if (saved === "da" || saved === "en") {
        return saved;
      }
    } catch {
      // Fall back to browser language.
    }
    return navigator.language.toLowerCase().startsWith("da") ? "da" : "en";
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    textarea.remove();
  }

  languageButtons.forEach((button) => {
    button.addEventListener("click", () => setLanguage(button.dataset.setLang));
  });

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = document.getElementById(button.dataset.copyTarget);
      if (!target) {
        return;
      }
      const original = button.innerHTML;
      try {
        await copyText(target.innerText.trim());
        button.textContent = root.dataset.lang === "da" ? "Kopieret" : "Copied";
        window.setTimeout(() => {
          button.innerHTML = original;
        }, 1500);
      } catch {
        button.textContent = root.dataset.lang === "da" ? "Fejlede" : "Failed";
        window.setTimeout(() => {
          button.innerHTML = original;
        }, 1500);
      }
    });
  });

  setLanguage(preferredLanguage());
})();
