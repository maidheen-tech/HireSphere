document.addEventListener("DOMContentLoaded", () => {
  const menuButton = document.querySelector(".menu-toggle");
  const navigation = document.querySelector(".main-nav");
  if (menuButton && navigation) {
    menuButton.addEventListener("click", () => {
      const isOpen = navigation.classList.toggle("is-open");
      menuButton.setAttribute("aria-expanded", String(isOpen));
      menuButton.textContent = isOpen ? "×" : "☰";
    });
  }

  document.querySelectorAll(".flash-close").forEach((button) => {
    button.addEventListener("click", () => button.closest(".flash")?.remove());
  });
  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
  document.querySelectorAll('input[type="file"][accept*="pdf"]').forEach((input) => {
    input.addEventListener("change", () => {
      const file = input.files?.[0];
      if (file && (file.size > 5 * 1024 * 1024 || !file.name.toLowerCase().endsWith(".pdf"))) {
        window.alert("Please choose a PDF file no larger than 5 MB.");
        input.value = "";
      }
    });
  });
  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      if (!form.checkValidity()) {
        event.preventDefault();
        form.reportValidity();
        return;
      }
      const submitter = event.submitter;
      if (submitter instanceof HTMLButtonElement && submitter.type === "submit" && !submitter.disabled) {
        submitter.disabled = true;
        submitter.dataset.originalText = submitter.textContent;
        submitter.textContent = "Working...";
        form.setAttribute("aria-busy", "true");
      }
    });
  });
});
