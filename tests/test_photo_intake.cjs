// Run using: node tests/test_photo_intake.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

async function test() {
  const photoTools = await import('../stocknote_photo.mjs');
  assert.equal(photoTools.parseQuantity('1,2'), null);
  assert.equal(photoTools.parseQuantity('0'), null);
  assert.deepEqual(photoTools.parseQuantity('１,２００ 켤레'), {qty: 1200, unit: '켤레'});

  const coordinates = [24, 240, 550, 715, 900];
  const word = (text, i, y) => [5, 1, 1, 1, 1, 1, coordinates[i], y, text.length * 14, 27, 95, text].join('\t');
  const tsv = [
    ['품번', '상품명', '사이즈', '입고수량', '비고'],
    ['NK-001', '운동화', '270', '3', '샘플'],
    ['00042', '후드티', 'M', '12', ''],
    ['합계', '', '', '15', ''],
  ].flatMap((row, i) => row.map((text, j) => text && word(text, j, 40 + 55 * i)).filter(Boolean)).join('\n');
  const rows = photoTools.parsePhotoTable(tsv);
  assert.deepEqual(rows, [
    {sku: 'NK-001', name: '운동화', option: '270', qty: '3'},
    {sku: '00042', name: '후드티', option: 'M', qty: '12'},
  ]);

  const main = fs.readFileSync(path.join(__dirname, '..', '재고노트_v7_범용재고관리.html'), 'utf8');
  const logic = main.slice(main.indexOf('      function planPhotoReceipt'), main.indexOf('      async function savePhotoReceipt'));
  let id = 0;
  const {planPhotoReceipt, preparePhotoReceipt} = vm.runInNewContext(logic + '\n({planPhotoReceipt, preparePhotoReceipt})', {
    photoTools,
    variantKey: (sku, option) => JSON.stringify([sku.trim().toUpperCase(), option.trim().toUpperCase()]),
    activeProducts: data => data.products.filter(product => !product.archived),
    categoryOf: () => '의류', defaultOptionName: () => '사이즈', LOW_STOCK_THRESHOLD: 5,
    validQty: qty => Number.isSafeInteger(qty) && qty > 0 && qty <= 999999999,
    id: () => 'new-' + ++id,
  });
  const existing = {id: 'original', sku: 'NK-001', name: '기존 상품명', option: '270', stock: 5, unit: '켤레', category: '신발'};
  const archived = {...existing, id: 'archived', option: '280', archived: true};
  const data = {products: [existing, archived], orders: [{id: 'order'}], aliases: {SKU: 'original'}, movements: [{id: 'history'}]};
  const intake = [...rows, {...rows[0], option: '265', qty: '2'}, {...rows[0], qty: '4'}];
  const plan = planPhotoReceipt(data, intake);
  assert.equal(plan[0].after, 8);
  assert.equal(plan[2].label, '새 옵션');
  assert.equal(plan[3].after, 12);
  const next = preparePhotoReceipt(data, intake);
  assert.equal(next.products[0].id, existing.id);
  assert.equal(next.products[0].name, existing.name);
  assert.equal(next.products[0].stock, 12);
  assert.equal(next.products[2].sku, '00042');
  assert.equal(next.products[2].stock, 12);
  assert.equal(next.products[3].stock, 2);
  assert.equal(next.products[3].unit, '켤레');
  assert.equal(next.movements.filter(move => move.kind === 'in').length, 4);
  assert.equal(next.movements.at(-1).id, 'history');
  assert.deepEqual(next.orders, data.orders);
  assert.equal(existing.stock, 5, 'Preparing a receipt must never modify the saved snapshot');
  assert.throws(() => preparePhotoReceipt(data, [{...rows[0], option: '280'}]), /복구한 뒤/);
  assert.throws(() => preparePhotoReceipt(data, [{...rows[0], qty: '999999999'}]), /999,999,999/);
  console.log('PASS: photo table fields and leading zeros, additive receipt, new SKU/option, preserved order history and validation');
}

test().catch(error => { console.error(error); process.exitCode = 1; });
