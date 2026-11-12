import { useState } from 'react';
import { Checkbox, Space } from 'antd';
import DeleteRecordsButton from './DeleteRecordsButton';

export default function ConversationList({items, current, busy, open, deleted}: {items: {id:string; title:string; detail:string}[]; current?:string; busy:boolean; open:(id:string)=>void; deleted:(ids:string[])=>void|Promise<void>}) {
  const [selected, setSelected] = useState<string[]>([]);
  const ids = selected.filter(id => items.some(item => item.id === id));
  return <>
    {!!items.length && <Space wrap className="section-bottom"><Checkbox disabled={busy} checked={ids.length === items.length} indeterminate={ids.length > 0 && ids.length < items.length} onChange={e => setSelected(e.target.checked ? items.map(i=>i.id) : [])}>全选</Checkbox>
      <DeleteRecordsButton ids={ids} disabled={busy} label={`删除选中（${ids.length}）`} onDeleted={async () => {await deleted(ids); setSelected([]);}} />
    </Space>}
    {items.map(item => <div key={item.id} style={{display:'flex',alignItems:'center',gap:6}}>
      <Checkbox aria-label={`选择记录：${item.title}`} disabled={busy} checked={ids.includes(item.id)} onChange={e => setSelected(previous => e.target.checked ? [...previous,item.id] : previous.filter(id=>id !== item.id))} />
      <button disabled={busy} className={`session-item ${current === item.id ? 'selected' : ''}`} onClick={()=>open(item.id)} style={{flex:1,minWidth:0}}>{item.title}<small>{item.detail}</small></button>
    </div>)}
  </>;
}
