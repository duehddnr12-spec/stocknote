'use strict';

const $ = id => document.getElementById(id);
const formatQty = new Intl.NumberFormat('ko-KR');
const tokenStorage = 'stocknote-mobile-access';
let token = location.hash.slice(1);
try {
  if (token) {
    sessionStorage.setItem(tokenStorage, token);
    history.replaceState(null, '', location.pathname);
  } else {
    token = sessionStorage.getItem(tokenStorage) || '';
  }
} catch {
  // The connection URL can also be used when private browsing blocks storage.
}
let page = 1, refreshTimer, currentRequest;

function element(tag, text, className = '') {
  const node = document.createElement(tag);
  node.textContent = text;
  node.className = className;
  return node;
}

function stockCard([sku, name, option, stock, available, unit]) {
  const item = element('li', '', 'item');
  item.append(element('p', sku, 'sku'), element('h2', name), element('p', option || '기본', 'option'));
  const quantities = element('dl', '', 'quantities');
  for (const [label, qty] of [['현재 재고', stock], ['판매 가능', available]]) {
    const group = document.createElement('div');
    group.append(element('dt', label), element('dd', formatQty.format(qty) + unit));
    quantities.append(group);
  }
  item.append(quantities);
  return item;
}

function showConnectionError(message) {
  $('status').className = 'status error';
  $('status').textContent = '연결 확인 필요';
  $('count').textContent = '';
  $('inventory').replaceChildren();
  $('pagination').hidden = true;
  $('empty').textContent = message;
  $('empty').hidden = false;
}

async function loadInventory() {
  clearTimeout(refreshTimer);
  currentRequest?.abort();
  if (!/^[A-Za-z0-9_-]{32}$/.test(token)) {
    showConnectionError('PC의 재고노트에서 휴대폰 조회를 켜고 연결 QR을 스캔해 주세요.');
    return;
  }
  const controller = new AbortController();
  currentRequest = controller;
  const timeout = setTimeout(() => controller.abort(), 4500);
  const q = $('search').value.trim();
  try {
    const params = new URLSearchParams({q, page: String(page)});
    const response = await fetch('/api/inventory?' + params, {
      headers: {Authorization: 'Bearer ' + token}, signal: controller.signal, cache: 'no-store', credentials: 'omit',
    });
    if (currentRequest !== controller) return;
    if (response.status === 401) {
      token = '';
      try { sessionStorage.removeItem(tokenStorage); } catch {}
      showConnectionError('연결이 만료됐어요. PC에 표시된 새 QR로 다시 연결해 주세요.');
      return;
    }
    if (!response.ok) throw Error('연결 확인 필요');
    const data = await response.json();
    if (currentRequest !== controller) return;
    page = data.page;
    const updated = new Date(data.updatedAt).toLocaleTimeString('ko-KR', {hour: '2-digit', minute: '2-digit', second: '2-digit'});
    $('status').className = 'status active';
    $('status').textContent = 'PC 반영 ' + updated;
    const count = formatQty.format(data.total) + '개 옵션';
    if ($('count').textContent !== count) $('count').textContent = count;
    $('empty').textContent = q ? '검색 결과가 없어요.' : '등록된 상품이 없어요.';
    $('empty').hidden = data.total > 0;
    $('inventory').replaceChildren(...data.rows.map(stockCard));
    $('pagination').hidden = data.pages <= 1;
    $('page').textContent = `${page} / ${data.pages}`;
    $('prev').disabled = page <= 1;
    $('next').disabled = page >= data.pages;
  } catch {
    if (currentRequest === controller) showConnectionError('PC에서 재고노트가 실행 중인지, 폰과 PC가 같은 Wi-Fi에 연결됐는지 확인해 주세요.');
  } finally {
    clearTimeout(timeout);
    if (currentRequest === controller && token && !document.hidden) {
      refreshTimer = setTimeout(loadInventory, 4000);
    }
  }
}

$('search').addEventListener('input', () => {
  page = 1;
  clearTimeout(refreshTimer);
  currentRequest?.abort();
  currentRequest = null;
  refreshTimer = setTimeout(loadInventory, 180);
});
$('prev').addEventListener('click', () => { page--; loadInventory(); window.scrollTo(0, 0); });
$('next').addEventListener('click', () => { page++; loadInventory(); window.scrollTo(0, 0); });
document.addEventListener('visibilitychange', () => {
  if (document.hidden) clearTimeout(refreshTimer);
  else loadInventory();
});
loadInventory();
