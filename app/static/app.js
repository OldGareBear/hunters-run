// Progressive enhancement only: every page works without JavaScript.
document.addEventListener("submit", (event) => {
  const form = event.target;
  const question = form.dataset.confirm;
  if (question && !window.confirm(question)) {
    event.preventDefault();
    return;
  }
  const busy = form.dataset.busy;
  const button = form.querySelector("button[type=submit], button:not([type])");
  if (button) {
    // Defer so the button's value is still submitted.
    setTimeout(() => {
      button.disabled = true;
      if (busy) button.textContent = busy;
    }, 0);
  }
});

document.addEventListener("change", (event) => {
  const el = event.target;
  if (el.matches("select[data-autosubmit]")) el.form.requestSubmit();
});

document.addEventListener("keydown", (event) => {
  // Enter sends a chat question; Shift+Enter adds a new line.
  const el = event.target;
  if (el.matches(".ask textarea") && event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el.form.requestSubmit();
  }
});
