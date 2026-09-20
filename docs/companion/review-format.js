'use strict';
(function(root){
 function cleanReview(value){
  if(!value||typeof value!=='object'||typeof value.id!=='string'||!/^[a-f0-9]{24}$/.test(value.id)||typeof value.markdown!=='string'||value.markdown.length>150000)throw new Error('This is not a supported Arcana review.');
  const text=(key,max)=>typeof value[key]==='string'?value[key].slice(0,max):'';
  return {id:value.id,markdown:value.markdown,result:text('result',100),finished_at:text('finished_at',100),turns:Number.isSafeInteger(value.turns)?value.turns:0,game_number:Number.isSafeInteger(value.game_number)?value.game_number:1,note:text('note',4000)};
 }
 function parse(text){
  if(typeof text!=='string'||text.length>2000000)throw new Error('Choose an Arcana file smaller than 2 MB.');
  let data;try{data=JSON.parse(text);}catch{throw new Error('This file is not valid JSON. Choose a file exported by Arcana.');}
  if(data.version!==1)throw new Error('This file version is not supported.');
  if(data.format==='arcana-review')return [cleanReview(data.review)];
  if(data.format==='arcana-pocket-backup'&&Array.isArray(data.reviews)&&data.reviews.length<=50)return data.reviews.map(cleanReview);
  throw new Error('Choose an Arcana review or Pocket backup.');
 }
 const api={parse,cleanReview};if(typeof module!=='undefined')module.exports=api;else root.ArcanaReview=api;
})(typeof window==='undefined'?{}:window);
