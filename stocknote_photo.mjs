const FIELD_LABELS = {
  sku: ['품번', '상품코드', '관리코드', '관리코드SKU', '제품코드', '상품번호', '모델번호', 'SKU', 'ITEMCODE', 'CODE'],
  name: ['상품명', '품명', '품목명', '제품명', '물품명', '상품이름', 'NAME', 'PRODUCTNAME', 'ITEMNAME'],
  option: ['사이즈', '옵션', '옵션1', '옵션2', '색상', '규격', 'SIZE', 'OPTION', 'COLOR'],
  qty: ['입고수량', '수량', '상품수량', '입고량', 'QTY', 'QUANTITY'],
  ignore: ['번호', '순번', 'NO', '일자', '입고일', '단위', '단가', '금액', '공급가액', '비고', '바코드', 'PRICE', 'AMOUNT', 'NOTE'],
};
const labelKey = text => text.normalize('NFKC').replace(/[^\p{L}\p{N}]/gu, '').toUpperCase();
const fields = new Map(Object.entries(FIELD_LABELS).flatMap(([key, labels]) => labels.map(label => [labelKey(label), key])));
const isTotal = text => /^(?:\d+[.)]\s*)?(?:합\s*계|소\s*계|총\s*계|총\s*수\s*량|SUBTOTAL\b|TOTAL\b)/i.test(text);

export function parseQuantity(text) {
  const normalized = String(text ?? '').normalize('NFKC').trim();
  const match = normalized.match(/^(\d+|\d{1,3}(?:,\d{3})+)(?:\s*(개|켤레|장|박스|팩|세트|봉|병|캔|권|kg|EA|PCS))?$/i);
  if (!match) return null;
  const qty = Number(match[1].replaceAll(',', ''));
  const unit = /^(EA|PCS)$/i.test(match[2] || '') ? '개' : match[2]?.toLowerCase() || '';
  return Number.isSafeInteger(qty) && qty >= 1 && qty <= 999999999 ? {qty, unit} : null;
}

function textLines(tsv) {
  const words = [];
  for (const line of String(tsv ?? '').split(/\r?\n/)) {
    const columns = line.split('\t');
    if (columns[0] !== '5' || columns.length < 12) continue;
    const [left, top, width, height] = columns.slice(6, 10).map(Number);
    const text = columns.slice(11).join(' ').trim();
    if (!text || /^\|+$/.test(text) || ![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) continue;
    words.push({left, right: left + width, height, middle: top + height / 2, text});
  }
  words.sort((a, b) => a.middle - b.middle || a.left - b.left);
  const lines = [];
  for (const word of words) {
    const last = lines.at(-1);
    if (last && Math.abs(last.middle - word.middle) <= Math.max(4, Math.min(last.height, word.height) * .58)) {
      last.middle += (word.middle - last.middle) / (last.words.length + 1);
      last.height = Math.max(last.height, word.height);
      last.words.push(word);
    } else lines.push({middle: word.middle, height: word.height, words: [word]});
  }
  for (const line of lines) line.words.sort((a, b) => a.left - b.left);
  return lines;
}

function readHeaders(line) {
  const result = [];
  for (let start = 0; start < line.words.length;) {
    let header;
    for (let length = 1; length <= 4 && start + length <= line.words.length; length++) {
      const words = line.words.slice(start, start + length);
      if (length > 1 && words.at(-1).left - words.at(-2).right > line.height) break;
      const key = fields.get(labelKey(words.map(word => word.text).join('')));
      if (key) header = {key, left: words[0].left, right: words.at(-1).right, length};
    }
    if (header) result.push(header);
    start += header?.length || 1;
  }
  const found = new Set(result.map(column => column.key));
  return found.has('sku') && found.has('qty') && (found.has('name') || found.has('option')) ? result : null;
}

function cleanRow(sku, name, option, qty) {
  return {
    sku: sku.trim().replace(/\s*([-_/])\s*/g, '$1'),
    name: name.trim(),
    option: option.trim(),
    qty: qty.trim(),
  };
}

export function parsePhotoTable(tsv, verticalRules = []) {
  const lines = textLines(tsv);
  const output = [];
  let columns = null, cuts = [];
  for (const line of lines) {
    const words = line.words;
    const header = readHeaders(line);
    if (header) {
      columns = header;
      cuts = columns.slice(1).map((column, index) => {
        const before = columns[index];
        const rule = verticalRules.filter(x => x > before.right && x < column.left).at(-1);
        return rule ?? Math.max((before.right + column.left) / 2, column.left - line.height * .6);
      });
      continue;
    }
    if (isTotal(words.map(word => word.text).join(' '))) continue;
    if (columns) {
      const cells = columns.map(() => []);
      let index = 0;
      for (const word of words) {
        while (index < cuts.length && (word.left + word.right) / 2 >= cuts[index]) index++;
        cells[index].push(word);
      }
      const joined = cells.map(cell => cell.map((word, i) => {
        const space = i && word.left - cell[i - 1].right >= line.height * .27 ? ' ' : '';
        return space + word.text;
      }).join(''));
      const get = (key, separator = ' ') => columns.flatMap((column, i) =>
        column.key === key && joined[i] ? [joined[i]] : []).join(separator);
      const row = cleanRow(get('sku'), get('name'), get('option', ' / '), get('qty'));
      if (row.sku || row.name || row.option || parseQuantity(row.qty)) output.push(row);
    } else {
      // Wide gaps in an unlabelled table can still identify its individual cells.
      const grouped = [];
      for (const [index, word] of words.entries()) {
        if (!index || word.left - words[index - 1].right > line.height * .9) grouped.push(word.text);
        else grouped[grouped.length - 1] += ' ' + word.text;
      }
      if (grouped.length === 4 && /[A-Z\d]/i.test(grouped[0]) && parseQuantity(grouped[3])) {
        output.push(cleanRow(...grouped));
      }
    }
  }
  return output;
}

export function straightenPhoto(canvas) {
  const scale = Math.min(1, 620 / Math.max(canvas.width, canvas.height));
  const sample = document.createElement('canvas');
  sample.width = Math.max(1, Math.round(canvas.width * scale));
  sample.height = Math.max(1, Math.round(canvas.height * scale));
  const {width, height} = sample;
  const context = sample.getContext('2d', {willReadFrequently: true});
  context.drawImage(canvas, 0, 0, width, height);
  const {data} = context.getImageData(0, 0, width, height);
  const xs = [], ys = [];
  for (let y = 2; y < height - 2; y++) {
    for (let x = 2; x < width - 2; x++) {
      const i = 4 * (y * width + x);
      if (data[i] + data[i + 1] + data[i + 2] < 590) { xs.push(x - width / 2); ys.push(y); }
    }
  }
  if (xs.length < 80 || xs.length > width * height * .32) return canvas;
  const padding = Math.ceil(width * .12);
  const projection = new Uint32Array(height + padding * 2);
  function score(angle) {
    projection.fill(0);
    const slope = Math.tan(angle * Math.PI / 180);
    for (let i = 0; i < xs.length; i++) projection[Math.round(ys[i] - xs[i] * slope) + padding]++;
    return projection.reduce((total, count) => total + count * count, 0);
  }
  const baseline = score(0);
  let best = baseline, angle = 0;
  for (let degrees = -11; degrees <= 11; degrees += .5) {
    const candidate = score(degrees);
    if (candidate > best) { best = candidate; angle = degrees; }
  }
  const initial = angle;
  for (const offset of [-.3, -.2, -.1, .1, .2, .3]) {
    const candidate = score(initial + offset);
    if (candidate > best) { best = candidate; angle = initial + offset; }
  }
  if (Math.abs(angle) < .3 || best < baseline * 1.06) return canvas;
  const radians = angle * Math.PI / 180, sin = Math.abs(Math.sin(radians)), cos = Math.cos(radians);
  const straight = document.createElement('canvas');
  straight.width = Math.ceil(canvas.width * cos + canvas.height * sin);
  straight.height = Math.ceil(canvas.height * cos + canvas.width * sin);
  const corrected = straight.getContext('2d', {willReadFrequently: true});
  corrected.fillStyle = '#fff';
  corrected.fillRect(0, 0, straight.width, straight.height);
  corrected.save();
  corrected.translate(straight.width / 2, straight.height / 2);
  corrected.rotate(-radians);
  corrected.drawImage(canvas, -canvas.width / 2, -canvas.height / 2);
  corrected.restore();
  return straight;
}

export function removeTableRules(canvas) {
  const {width, height} = canvas;
  const context = canvas.getContext('2d', {willReadFrequently: true});
  const image = context.getImageData(0, 0, width, height);
  const pixels = image.data;
  const rules = [];
  for (const [count, length, outerStep, innerStep] of [[width, height, 4, width * 4], [height, width, width * 4, 4]]) {
    const minimum = Math.max(70, length * .23);
    const gap = Math.max(5, Math.floor(length * .02));
    for (let axis = 0; axis < count; axis++) {
      let start = -1, last = -1, dark = 0;
      const offset = axis * outerStep;
      for (let cell = 0; cell <= length; cell++) {
        const i = offset + cell * innerStep;
        if (cell < length && pixels[i] + pixels[i + 1] + pixels[i + 2] < 610) {
          if (start === -1) start = cell;
          last = cell;
          dark++;
        } else if (start !== -1 && (cell - last > gap || cell === length)) {
          const end = last + 1;
          if (end - start >= minimum && dark >= (end - start) * .65) rules.push({axis, offset, innerStep, start, end});
          start = -1;
          dark = 0;
        }
      }
    }
  }
  for (const {offset, innerStep, start, end} of rules) {
    for (let cell = start; cell < end; cell++) {
      const i = offset + cell * innerStep;
      pixels[i] = pixels[i + 1] = pixels[i + 2] = 255;
    }
  }
  if (rules.length) context.putImageData(image, 0, 0);
  return [...new Set(rules.filter(rule => rule.innerStep === width * 4).map(rule => rule.axis))];
}

let engineScript;
function loadEngine() {
  engineScript ??= new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = '/ocr/tesseract.min.js';
    script.onload = () => resolve(globalThis.Tesseract);
    script.onerror = () => reject(Error('설치 파일의 사진 인식 엔진을 읽을 수 없어요.'));
    document.head.append(script);
  }).catch(error => { engineScript = null; throw error; });
  return engineScript;
}

export async function recognizePhoto(file, logger) {
  if (!/\.(jpe?g|png|webp)$/i.test(file.name) || file.size > 15 * 1024 * 1024) {
    throw Error('15MB 이하의 JPG, PNG 또는 WEBP 사진을 선택해 주세요.');
  }
  const image = new Image();
  const url = URL.createObjectURL(file);
  const canvas = document.createElement('canvas');
  try {
    image.src = url;
    await image.decode();
    const scale = Math.min(2, 2650 / Math.max(image.naturalWidth, image.naturalHeight));
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext('2d', {willReadFrequently: true});
    context.fillStyle = '#fff';
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
  } finally {
    URL.revokeObjectURL(url);
  }

  const engine = await loadEngine();
  const prepared = straightenPhoto(canvas);
  const rules = removeTableRules(prepared);
  const base = new URL('/ocr/', location.href).href;
  let worker, rejectRecognition;
  const failure = new Promise((_, reject) => { rejectRecognition = reject; });
  const timeout = setTimeout(() => rejectRecognition(Error('사진 인식 시간이 길어졌어요. 표 부분만 잘라 다시 시도해 주세요.')), 90000);
  const starting = engine.createWorker(['kor', 'eng'], 1, {
    workerPath: base + 'worker.min.js', corePath: base, langPath: base,
    workerBlobURL: false, cacheMethod: 'none', logger,
    errorHandler: reason => rejectRecognition(Error(String(reason))),
  });
  try {
    worker = await Promise.race([starting, failure]);
    await Promise.race([worker.setParameters({tessedit_pageseg_mode: '6'}), failure]);
    let {data} = await Promise.race([worker.recognize(prepared, {}, {tsv: true, text: true}), failure]);
    let rows = parsePhotoTable(data.tsv, rules);
    if (!rows.length) {
      await Promise.race([worker.setParameters({tessedit_pageseg_mode: '11'}), failure]);
      const retry = await Promise.race([worker.recognize(prepared, {}, {tsv: true, text: true}), failure]);
      if (retry.data.text.trim()) data = retry.data;
      rows = parsePhotoTable(data.tsv, rules);
    }
    return {rows, text: data.text.trim()};
  } finally {
    clearTimeout(timeout);
    if (worker) await worker.terminate();
    else starting.then(instance => instance.terminate()).catch(() => {});
  }
}
