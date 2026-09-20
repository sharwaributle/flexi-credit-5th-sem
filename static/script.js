document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("chat-form");
  if (!form) return;

  const input = document.getElementById("chat-input");
  const windowEl = document.getElementById("chat-window");

  const scrollToBottom = () => {
    windowEl.scrollTop = windowEl.scrollHeight;
  };
  scrollToBottom();

  const addMessage = (role, text) => {
    const empty = windowEl.querySelector(".empty-state");
    if (empty) empty.remove();

    const wrap = document.createElement("div");
    wrap.className = `chat-msg chat-msg--${role}`;
    const label = document.createElement("span");
    label.className = "chat-msg-role";
    label.textContent = role === "user" ? "You" : "Ledger";
    const p = document.createElement("p");
    p.textContent = text;
    wrap.appendChild(label);
    wrap.appendChild(p);
    windowEl.appendChild(wrap);
    scrollToBottom();
  };

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const message = input.value.trim();
    if (!message) return;

    addMessage("user", message);
    input.value = "";
    input.disabled = true;

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      });
      const data = await res.json();
      if (data.error) {
        addMessage("assistant", `Error: ${data.error}`);
      } else {
        addMessage("assistant", data.reply);
      }
    } catch (err) {
      addMessage("assistant", "Something went wrong reaching the server.");
    } finally {
      input.disabled = false;
      input.focus();
    }
  });
});
