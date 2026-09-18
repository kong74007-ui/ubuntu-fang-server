import fs from 'node:fs';
import path from 'node:path';
import {inlineSubCompositions,parseHTMLContent,prepareFlattenedInnerRoot,assignBundledRuntimeCompositionIds} from '@hyperframes/core/compiler';

export function compile(project) {
 const local=name=>{
  const file=fs.realpathSync(path.resolve(project,name)),relative=path.relative(project,file);
  if(relative.startsWith('..')||path.isAbsolute(relative))throw Error('Out-of-project sub-composition');
  return file;
 };
 const doc=parseHTMLContent(fs.readFileSync(local('index.html'),'utf8'));
 const counts=new Map();let levels=0;
 while(doc.querySelector('[data-composition-src]')){
  if(++levels>8)throw Error('Sub-composition nesting limit exceeded');
  const hosts=Array.from(doc.querySelectorAll('[data-composition-src]'));
  const result=inlineSubCompositions(doc,hosts,{
   resolveHtml:src=>fs.readFileSync(local(src),'utf8'),parseHtml:parseHTMLContent,
   hostIdentityMap:assignBundledRuntimeCompositionIds(hosts,counts),
   flattenInnerRoot:prepareFlattenedInnerRoot,rewriteInlineStyles:true,
   assetExists:src=>{try{return fs.statSync(local(src)).isFile();}catch{return false;}},
   onMissingComposition:src=>{throw Error('Unresolved sub-composition '+src);},
  });
  for(const css of result.styles){const style=doc.createElement('style');style.textContent=css;doc.head.appendChild(style);}
  for(const script of result.scriptItems){const el=doc.createElement('script');if(script.kind==='inline')el.textContent=script.content;else el.setAttribute('src',script.src);doc.body.appendChild(el);}
  for(const link of result.externalLinks){const el=doc.createElement('link');el.setAttribute('rel',link.rel);el.setAttribute('href',link.href);doc.head.appendChild(el);}
 }
 return '<!DOCTYPE html>'+doc.documentElement.outerHTML;
}
