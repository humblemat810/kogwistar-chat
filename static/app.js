(function () {
  function scrollChatToBottom() {
    const chatWindow = document.getElementById('chat-window');
    if (chatWindow) {
      chatWindow.scrollTop = chatWindow.scrollHeight;
    }
  }

  document.addEventListener('htmx:afterSwap', function (evt) {
    const target = evt && evt.detail && evt.detail.target;
    if (!target) return;
    if (target.id === 'chat-window' || (target.closest && target.closest('#chat-window'))) {
      scrollChatToBottom();
    }
  });

  document.addEventListener('DOMContentLoaded', scrollChatToBottom);
})();
