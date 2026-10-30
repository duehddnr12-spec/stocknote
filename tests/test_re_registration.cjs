// Regression tests for previously archived products and SKU variants.
// Run using: node tests/test_re_registration.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '..', '재고노트_v7_범용재고관리.html'), 'utf8');
const block = html.match(/<script[^>]*>([\s\S]*?)<\/script>/i);
assert(block, 'Expected one inline application script');
new vm.Script(block[1]); // Detect syntax errors before packaging.
const extract = html.match(/function restoreVariantForRegistration\(p,changes,targetStock\)\{[\s\S]*?\n      \}/);
assert(extract, 'Product revival helper must exist');
const revive = vm.runInNewContext('(' + extract[0] + ')');
function archived(stock, byProduct = true) {
  return { id:'kept-original-id', sku:'TESTSHOE001',option:'270',stock,archived:true,
    archivedAt:'2026-10-01',archivedByProduct:byProduct,name:'original' };
}
{
 const p=archived(9);
 const r=revive(p,{name:'re-registered'},3);
 assert.deepEqual([p.stock,p.archived,p.archivedByProduct,p.archivedAt,p.id],
                  [3,false,false,'','kept-original-id']);
 assert.equal(r.delta,-6,'Never silently sum old archived stock and new entered stock');
 assert.equal(r.wasProductArchived,true);
 assert.equal(r.wasArchived,true);
}
{
 const p=archived(0,false);
 const r=revive(p,{name:'size returned'},5);
 assert.equal(p.stock,5);assert.equal(r.delta,5);
 assert.equal(r.wasProductArchived,false);
}
{
 const p=archived(9);
 const r=revive(p,{},undefined);
 assert.equal(p.stock,9,'When Excel omits quantity, preserve saved quantity');
 assert.equal(r.delta,0);
}
{
 const p={ id:'live',stock:4,archived:false,archivedByProduct:false };
 const r=revive(p,{name:'unchanged'},undefined);
 assert.equal(p.stock,4);assert.equal(r.wasArchived,false);
}
assert(!html.includes("if(exists?.archivedByProduct)throw Error"),
  'Excel import must not reject a previously deleted product');
assert(html.includes("restoreVariantForRegistration(existingVariant,base,Number(numbers.initial))"),
  'Manual registration must revive product option');
assert(html.includes("restoreVariantForRegistration(p,changes,targetStock)"),
  'Excel import must revive product option');
assert(html.includes("if(restored.delta!==0)bulkLog(p,'adjust'"),
  'Excel re-registration changes must be auditable');
console.log('PASS: deleted SKU/option re-registration, existing quantities, history IDs, Excel path, JS syntax');
