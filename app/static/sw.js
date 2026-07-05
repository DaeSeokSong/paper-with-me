/* paper-with-me 서비스워커 — 오프라인 셸 + 정적 자원 캐시.
   데이터 페이지는 network-first(신선도 우선), 실패 시 오프라인 안내. */
const CACHE = "pwm-shell-v2"; /* v2: 에러 응답 캐시 버그 축출 + vendor 자산 */
const SHELL = ["/static/offline.html", "/static/icon.svg",
               "/static/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/static/")) {
    // 정적 자원: cache-first
    e.respondWith(
      caches.match(e.request).then((hit) => hit ||
        fetch(e.request).then((res) => {
          /* 5xx/부분 응답을 캐시하면 그 클라이언트는 영구적으로 깨진
             자산을 본다 (배포 재시작 중 요청 등) — 정상 응답만 저장 */
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE).then((c) => c.put(e.request, copy));
          }
          return res;
        }))
    );
    return;
  }
  if (e.request.mode === "navigate") {
    // 페이지: network-first, 오프라인이면 셸 안내
    e.respondWith(
      fetch(e.request).catch(() => caches.match("/static/offline.html"))
    );
  }
});
