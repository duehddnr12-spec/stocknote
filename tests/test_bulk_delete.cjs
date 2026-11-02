// Bulk archive regression coverage. Run: node tests/test_bulk_delete.cjs
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const html=fs.readFileSync(path.join(__dirname,'..','재고노트_v7_범용재고관리.html'),'utf8');
const script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/i)?.[1];
assert(script,'No inline script');new vm.Script(script);
function extracted(name){
 const re=new RegExp('function '+name+'\\([^\\n]*\\)\\{[\\s\\S]*?\\n      \\}');
 const match=script.match(re);
 assert(match,name+' not found');
 return match[0];
}
let seq=0;
const prepare=vm.runInNewContext('('+extracted('prepareArchivedDeletion')+')',{Set,Map,id:()=>String(++seq)});
const blocked=vm.runInNewContext('('+extracted('pendingLinkedSkuKeys')+')',{
 Set,Map,
 variantKey:(sku,opt)=>String(sku).trim().toUpperCase()+'|'+String(opt||'').trim().toUpperCase(),
 orderVariantAlias:o=>'VARIANT|'+o.channel+'|'+String(o.sku).trim().toUpperCase()+'|'+String(o.option||'').trim().toUpperCase(),
 orderKey:(channel,sku)=>channel+'|'+sku,
 normalizeStatus:s=>s==='취소완료'?'cancel':s==='반품완료'?'returned':'active'
});
const total=10000;
const products=Array.from({length:total},(_,i)=>({
 id:'original-'+i,sku:'SKU'+i,name:'상품 '+i,option:'',stock:3,unit:'개',
 archived:false,archivedByProduct:false
}));
const earlier={id:'old-receipt',sku:'SKU0',kind:'in',productId:'original-0'};
const state={products,movements:[earlier],orders:[],aliases:{}};
const keys=products.map(p=>p.sku);
const start=Date.now();
assert.equal(blocked(state).size,0);
const next=prepare(state,keys,'2026-11-02T00:00:00.000Z');
const elapsed=Date.now()-start;
assert(elapsed<5000,'10,000-record deletion preparation took too long: '+elapsed+'ms');
assert.equal(next.products.length,total);
assert(next.products.every(p=>p.archived&&p.archivedByProduct));
assert(products.every(p=>!p.archived),'Must not mutate original on a failed snapshot save');
assert.equal(next.movements.length,total+1);
assert.equal(next.movements.at(-1).id,earlier.id,'Old history must be preserved');
assert.equal(new Set(next.movements.slice(0,total).map(m=>m.sku)).size,total,'Each SKU needs an audit record');
assert.equal(next.products[0].stock,3,'Soft delete preserves stock');
assert.equal(next.products[0].id,'original-0','Soft delete preserves IDs');
const pendingState={...state,orders:[
 {channel:'스마트스토어',sku:'SKU9999',option:'',status:'결제완료',fulfillment:'ready',appliedQty:0},
 {channel:'스마트스토어',sku:'SKU2000',option:'',status:'취소완료',fulfillment:'ready',appliedQty:0},
 {channel:'스마트스토어',sku:'UNKNOWN',option:'',mappedProductId:'original-1500',status:'결제완료',fulfillment:'ready',appliedQty:0},
 {channel:'스마트스토어',sku:'SKU9000',option:'',status:'결제완료',fulfillment:'shipping',appliedQty:1}
]};
const linked=blocked(pendingState);
assert(linked.has('SKU9999'),'Pending order must block linked SKU');
assert(linked.has('SKU1500'),'Mapped ID must also block linked SKU');
assert(!linked.has('SKU2000'),'Canceled orders do not block deletion');
assert(!linked.has('SKU9000'),'Shipped orders do not block deletion');
assert(html.includes('entries.slice((archivedPage-1)*100,archivedPage*100)'),'Archived rows must be paged');
assert(!html.includes('next.movements.unshift({id:id(),productId:p.id,sku:p.sku,name:p.name,option:\'\',kind:\'product_delete\''),'No quadratic array.unshift per SKU');
console.log('PASS: 10,000 SKU archive in '+elapsed+' ms, safe pending order check, preserved stock and history, paged archive UI');
