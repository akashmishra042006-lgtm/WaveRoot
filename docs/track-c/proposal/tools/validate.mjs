// Package checks only. Does not compile RTL or run WaveRoot.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const read=p=>JSON.parse(fs.readFileSync(path.join(root,p),'utf8'));
const schema=read('contracts/report.schema.json');
let passed=0;
function check(name,fn){fn();passed++;console.log('PASS '+name);}
// Small validator for exactly the keywords used by report.schema.json.
// It deliberately rejects unknown schema keywords. Not a general JSON Schema library.
const known=new Set(['$schema','title','description','$defs','$ref','type','properties','required','additionalProperties','anyOf','const','enum','minimum','minLength','pattern','items']);
function auditSchema(s){
 for(const key of Object.keys(s))assert(known.has(key),'unsupported schema keyword '+key);
 for(const v of Object.values(s.$defs??{}))auditSchema(v);
 for(const v of Object.values(s.properties??{}))auditSchema(v);
 for(const v of s.anyOf??[])auditSchema(v);
 if(s.items)auditSchema(s.items);
 if(s.$ref)assert(schema.$defs[s.$ref.split('/').at(-1)],'unresolved schema ref');
}
function shape(value,s,where='$'){
 if(s.$ref)return shape(value,schema.$defs[s.$ref.split('/').at(-1)],where);
 if(s.anyOf){assert(s.anyOf.some(a=>{try{shape(value,a,where);return true;}catch{return false;}}),where+' does not match any allowed shape');return;}
 if('const' in s)assert.deepEqual(value,s.const,where+' const');
 if(s.enum)assert(s.enum.includes(value),where+' enum');
 if(s.type==='null')assert.equal(value,null,where);
 if(s.type==='object'){
  assert(value&&typeof value==='object'&&!Array.isArray(value),where+' object');
  for(const k of s.required??[])assert(Object.hasOwn(value,k),where+' missing '+k);
  if(s.additionalProperties===false)for(const k of Object.keys(value))assert(Object.hasOwn(s.properties,k),where+' unexpected '+k);
  for(const [k,v] of Object.entries(value))if(s.properties?.[k])shape(v,s.properties[k],where+'.'+k);
 }
 if(s.type==='array'){assert(Array.isArray(value),where+' array');value.forEach((v,i)=>shape(v,s.items,where+'['+i+']'));}
 if(s.type==='string'){assert.equal(typeof value,'string',where+' string');if(s.minLength)assert(value.length>=s.minLength,where+' length');if(s.pattern)assert(new RegExp(s.pattern).test(value),where+' pattern');}
 if(s.type==='integer'){assert(Number.isSafeInteger(value),where+' safe integer');if(s.minimum!==undefined)assert(value>=s.minimum,where+' minimum');}
}
function unique(items,key='id'){const ids=items.map(x=>x[key]);assert.equal(new Set(ids).size,ids.length,'duplicate '+key);return new Map(items.map(x=>[x[key],x]));}
function logic(v,width){if(v===null)return;assert.equal(v.width,v.bits.length,'bit width mismatch');if(width!==undefined)assert.equal(v.width,width,'signal width mismatch');}
function validate(r){
 shape(r,schema);
 const nodes=unique(r.nodes),edges=unique(r.edges),signals=unique(r.waveform.signals),sources=unique(r.sources);
 unique(r.suspects);unique(r.assertions);unique(r.suspects,'rank');
 const start=BigInt(r.waveform.start_tick),end=BigInt(r.waveform.end_tick);
 assert(start<end,'empty/reversed window');
 const inWindow=t=>assert(BigInt(t)>=start&&BigInt(t)<=end,'time outside window');
 function location(l){if(l===null)return;const s=sources.get(l.source_id);assert(s,'missing source');assert(l.line<=l.end_line,'reversed source span');assert(l.end_line<=s.content.trimEnd().split('\n').length,'source span outside content');}
 for(const s of sources.values()){
  assert(!path.isAbsolute(s.path)&&!s.path.includes('..')&&!s.path.includes('\\')&&!s.path.includes(':'),'unsafe source path');
  assert.equal(crypto.createHash('sha256').update(s.content).digest('hex'),s.sha256,'source hash mismatch');
 }
 for(const s of signals.values()){
  if(s.availability==='missing'){assert.equal(s.initial,null);assert.equal(s.transitions.length,0);}else assert(s.initial,'available signal needs left-boundary value');
  logic(s.initial,s.width);let prev=start;
  for(const t of s.transitions){inWindow(t.time_tick);assert(BigInt(t.time_tick)>prev,'non-increasing transitions');logic(t.value,s.width);prev=BigInt(t.time_tick);}
 }
 for(const n of nodes.values()){assert(signals.has(n.signal_id),'node signal missing');inWindow(n.observation.time_tick);logic(n.value,signals.get(n.signal_id).width);location(n.location);}
 for(const e of edges.values()){
  assert(nodes.has(e.source)&&nodes.has(e.target),'dangling graph edge');
  assert(BigInt(nodes.get(e.source).observation.time_tick)<=BigInt(nodes.get(e.target).observation.time_tick),'backwards causal edge');
 }
 r.suspects.forEach((s,i)=>{assert.equal(s.rank,i+1,'non-contiguous rank');assert(nodes.has(s.node_id),'dangling suspect node');s.evidence_node_ids.forEach(n=>assert(nodes.has(n),'dangling evidence'));location(s.location);});
 for(const a of r.assertions)if(a.validation==='validated')assert(a.tool&&a.result_ref,'validated assertion needs evidence');
 if(r.status==='pass'){assert.equal(r.failure,null);assert.equal(r.suspects.length,0);}
 if(r.status==='fail')assert(r.failure,'failed report requires failure');
 if(['inconclusive','unsupported','error'].includes(r.status))assert(r.diagnostics.length,'incomplete status requires explanation');
 if(r.failure){
  const f=r.failure;assert(signals.has(f.signal_id),'failure signal missing');inWindow(f.observation.time_tick);logic(f.expected,signals.get(f.signal_id).width);logic(f.actual,signals.get(f.signal_id).width);
  if(f.kind==='missing_transaction')assert.equal(f.actual,null,'missing transaction cannot have actual value');
  if(f.kind==='unknown_value')assert(f.actual&&/[xz]/.test(f.actual.bits),'unknown failure needs unknown bits');
 }
 return true;
}
check('schema uses only supported validation keywords',()=>auditSchema(schema));
const files=fs.readdirSync(path.join(root,'fixtures')).filter(f=>f.startsWith('report-')&&f.endsWith('.json'));
for(const f of files)check('schema and semantic invariants: '+f,()=>validate(read('fixtures/'+f)));
const fifo=read('fixtures/report-fifo.json');
const invalid=(name,mutate)=>check('reject '+name,()=>{const r=structuredClone(fifo);mutate(r);assert.throws(()=>validate(r));});
invalid('wrong bit width',r=>r.nodes[0].value.width=2);
invalid('dangling edge',r=>r.edges[0].source='no-such-node');
invalid('dangling suspect',r=>r.suspects[0].node_id='no-such-node');
invalid('duplicate node ID',r=>r.nodes.push(structuredClone(r.nodes[0])));
invalid('unordered transitions',r=>r.waveform.signals[0].transitions.reverse());
invalid('mismatched source revision',r=>r.sources[0].content+='changed\n');
invalid('source traversal',r=>r.sources[0].path='../other.sv');
invalid('unknown schema version',r=>r.schema_version='v99');
invalid('pass with a failure',r=>r.status='pass');
invalid('numeric absolute timestamp',r=>r.failure.observation.time_tick=35001);
invalid('unknown failure with known bits',r=>r.failure.kind='unknown_value');
invalid('out-of-range source span',r=>r.suspects[0].location.end_line=1000);
check('timestamps above 2^53 preserve one-tick distinction',()=>{const r=read('fixtures/report-large-time.json');const a=BigInt(r.waveform.start_tick);assert(a>BigInt(Number.MAX_SAFE_INTEGER));assert.equal(BigInt(String(a+1n))-BigInt(String(a)),1n);});
check('FIFO UI selection references exact signal, source, and observation',()=>{const n=fifo.nodes.find(n=>n.id===fifo.suspects[0].node_id);assert.equal(n.signal_id,'tb.dut.wptr');assert.deepEqual(n.location,fifo.suspects[0].location);assert.equal(n.observation.cycle,3);assert.equal(n.observation.phase,'post_settle');assert.equal(fifo.failure.signal_id,'tb.dut.full');});

const vectors=read('fixtures/benchmark-vectors.json');
check('FIFO directed oracle and pointer mutation',()=>{
 let q=[],transfers=[],occupancy=[],w=0,r=0,first=null;
 vectors.fifo.inputs.forEach((x,c)=>{
  const pop=q.length>0&&!!x.ready,push=!!x.valid&&(q.length<4||pop);
  if(pop)transfers.push(q.shift());if(push)q.push(x.data);occupancy.push(q.length);
  // Independently model the mutant's status/acceptance from wrapped pointers.
  const empty=w===r,full=((w&3)===(r&3))&&((w&4)!==(r&4));
  const badPop=!empty&&!!x.ready,badPush=!!x.valid&&(!full||badPop);
  if(badPop)r=(r+1)&7;if(badPush)w=(w&3)===3?(w&4):((w+1)&7);
  const badFull=Number(((w&3)===(r&3))&&((w&4)!==(r&4)));
  const goodFull=Number(q.length===4);
  if(first===null&&badFull!==goodFull)first={cycle:c,signal:'full',expected:goodFull,actual:badFull};
 });
 assert.deepEqual(occupancy,vectors.fifo.expected_occupancy);assert.deepEqual(transfers,vectors.fifo.expected_transfers);assert.deepEqual(first,vectors.fifo.first_mutant_mismatch);
});
check('elastic-buffer oracle, stalls, and lost-valid mutation',()=>{
 let full=0,data=null,badFull=0,badData=null,first=null,valids=[],datas=[],transfers=[];
 vectors.elastic.inputs.forEach((x,c)=>{
  if(full&&x.ready)transfers.push(data);
  if(!full||x.ready){full=x.valid;if(x.valid)data=x.data;}
  if(!badFull||x.ready){badFull=x.valid;if(x.valid)badData=x.data;}else if(!x.ready)badFull=0;
  valids.push(full);datas.push(full?data:null);
  if(first===null&&full!==badFull)first={cycle:c,signal:'m_valid',expected:full,actual:badFull};
 });
 assert.deepEqual(valids,vectors.elastic.expected_post_valid);assert.deepEqual(datas,vectors.elastic.expected_post_data);assert.deepEqual(transfers,vectors.elastic.expected_transfers);assert.deepEqual(first,vectors.elastic.first_mutant_mismatch);
});
check('signed MAC oracle, output latency, and premature narrowing',()=>{
 let acc=0,bad=0,prevValid=0,first=null,accs=[],outputs=[];
 vectors.mac.inputs.forEach((x,c)=>{
  const out=prevValid?acc:null,badOut=prevValid?bad:null;outputs.push(out);
  if(first===null&&out!==badOut)first={cycle:c,signal:'out_data',expected:out,actual:badOut};
  if(x.valid){acc=Math.max(-32768,Math.min(32767,acc+x.a*x.b));bad=Number(BigInt.asIntN(16,BigInt(bad+x.a*x.b)));}
  accs.push(acc);prevValid=x.valid;
 });
 assert.deepEqual(accs,vectors.mac.expected_acc);assert.deepEqual(outputs,vectors.mac.expected_outputs);assert.deepEqual(first,vectors.mac.first_mutant_mismatch);
 assert.equal((-32768>>>0).toString(2).slice(-16),'1000000000000000');assert.equal((17536).toString(2).padStart(16,'0'),'0100010010000000');
});
check('four-state mux oracle and equal-operand resolution',()=>{
 let sel=0,badSel=null,good=[],bad=[],first=null;
 const bits=n=>n.toString(2).padStart(8,'0');
 vectors.xprop.inputs.forEach((x,c)=>{
  const a=bits(x.a),b=bits(x.b);
  const out=x.valid?(sel?b:a):null;
  const mutant=x.valid?(badSel===null?[...a].map((v,i)=>v===b[i]?v:'x').join(''):(badSel?b:a)):null;
  good.push(out);bad.push(mutant);
  if(first===null&&out!==mutant)first={cycle:c,signal:'out_data',expected:out,actual:mutant};
  if(x.cfg_we){sel=x.cfg_sel;badSel=x.cfg_sel;}
 });
 assert.deepEqual(good,vectors.xprop.expected_outputs);assert.deepEqual(bad,vectors.xprop.expected_mutant_outputs);assert.deepEqual(first,vectors.xprop.first_mutant_mismatch);
});
check('relative Markdown links resolve',()=>{
 function walk(dir){for(const entry of fs.readdirSync(dir,{withFileTypes:true})){const f=path.join(dir,entry.name);if(entry.isDirectory())walk(f);else if(f.endsWith('.md')){for(const match of fs.readFileSync(f,'utf8').matchAll(/\]\(([^)]+)\)/g)){const target=match[1];if(!/^https?:|^#/.test(target))assert(fs.existsSync(path.resolve(path.dirname(f),target.split('#')[0])),f+': '+target);}}}}walk(root);
});
console.log(`\n${passed} package checks passed. No HDL simulator or WaveRoot implementation was run.`);
