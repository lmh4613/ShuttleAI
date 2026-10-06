self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_error) {
    payload = {};
  }

  const title = payload.title || "ShuttleAI 알림";
  const receivedTime = new Date().toLocaleTimeString("ko-KR", {
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const body = payload.include_received_time
    ? `${payload.body || "새로운 셔틀 알림이 있습니다."}\n수신: ${receivedTime}`
    : payload.body || "새로운 셔틀 알림이 있습니다.";
  const options = {
    body,
    icon: payload.icon || undefined,
    badge: payload.badge || undefined,
    data: { url: payload.url || self.location.origin + "/" },
    tag: "shuttleai-web-push-poc",
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = event.notification.data && event.notification.data.url
    ? event.notification.data.url
    : self.location.origin + "/";

  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
      for (const client of windows) {
        if (client.url === targetUrl && "focus" in client) return client.focus();
      }
      return clients.openWindow ? clients.openWindow(targetUrl) : undefined;
    })
  );
});
