'use strict';
const $=id=>document.getElementById(id),storageKey='arcana-pocket-v1';let reviews=[],selected='';
function status(text){$('status').textContent=text;}
try{const saved=localStorage.getItem(storageKey);if(saved)reviews=ArcanaReview.parse(saved);}catch{status('Your saved journal could not be opened. Import a backup to recover it.');}
function store(next){const serialized=JSON.stringify({format:'arcana-pocket-backup',version:1,reviews:next});if(serialized.length>2000000)throw new Error('This journal is full. Export a backup, then remove an older review.');localStorage.setItem(storageKey,serialized);reviews=next;}
function render(){
 const query=$('search').value.toLowerCase();const list=$('reviews');list.replaceChildren();
 const filtered=reviews.filter(r=>(r.markdown+' '+r.note).toLowerCase().includes(query));
 if(!filtered.length){const p=document.createElement('p');p.textContent=reviews.length?'No reviews match that search.':'No reviews yet. Import a completed desktop review to begin.';list.append(p);}
 for(const r of filtered){const b=document.createElement('button');b.className='review';b.setAttribute('aria-pressed',String(r.id===selected));b.textContent=r.result||'Completed game';const small=document.createElement('small');small.textContent=`${r.finished_at.slice(0,10)} · ${r.turns} turns · Game ${r.game_number}`;b.append(small);b.onclick=()=>{selected=r.id;render();};list.append(b);}
 const review=reviews.find(r=>r.id===selected);$('detail').hidden=!review;if(review){$('review-title').textContent=review.result||'Match review';$('review-text').textContent=review.markdown;$('note').value=review.note||'';}
 $('backup').disabled=!reviews.length;
}
$('import').onchange=async e=>{try{const file=e.target.files[0];if(!file)return;if(file.size>2000000)throw new Error('Choose a file smaller than 2 MB.');const incoming=ArcanaReview.parse(await file.text());const next=new Map(reviews.map(r=>[r.id,r]));for(const r of incoming){const old=next.get(r.id);next.set(r.id,{...r,note:old?.note||r.note});}if(next.size>50)throw new Error('Keep up to 50 reviews. Export a backup and remove older reviews first.');store([...next.values()]);selected=incoming[0]?.id||selected;render();status('Imported. Your review stays on this device.');}catch(error){status(error.message);}finally{e.target.value='';}};
$('search').oninput=render;
$('save-note').onclick=()=>{try{store(reviews.map(r=>r.id===selected?{...r,note:$('note').value.slice(0,4000)}:r));status('Reflection saved on this device.');}catch{status('Could not save. Your browser storage may be full or unavailable.');}};
$('remove').onclick=()=>{if(!confirm('Delete this review and its reflection from this browser? Export a backup first if you need a copy.'))return;try{store(reviews.filter(r=>r.id!==selected));selected='';render();status('Review deleted from this device.');}catch{status('Could not delete the review.');}};
$('backup').onclick=()=>{const blob=new Blob([JSON.stringify({format:'arcana-pocket-backup',version:1,reviews},null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='Arcana-Pocket-backup.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);status('Backup created. Keep it somewhere private.');};
render();if('serviceWorker'in navigator)navigator.serviceWorker.register('./sw.js').catch(()=>status('Offline installation is unavailable here. Your journal still works while this page is open.'));
