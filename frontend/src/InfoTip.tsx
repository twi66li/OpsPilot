import { Tooltip } from 'antd';
import { InfoCircleOutlined } from '@ant-design/icons';
import { useState } from 'react';

export default function InfoTip({label, children}: {label: string; children: React.ReactNode}) {
  const [open,setOpen]=useState(false);
  // 直接显示说明，避免“减少动态效果”的全局动画规则中断弹层定位。
  return <Tooltip title={children} open={open} onOpenChange={setOpen} trigger={['hover', 'focus', 'click']} mouseEnterDelay={0} mouseLeaveDelay={0} motion={{motionName:''}} destroyOnHidden>
    <button type="button" className="info-tip" aria-label={label} title={label} onClick={event=>{event.stopPropagation();setOpen(value=>!value);}} onKeyDown={event=>{if(event.key==='Escape'&&open){event.stopPropagation();setOpen(false);}}}><InfoCircleOutlined aria-hidden="true"/></button>
  </Tooltip>;
}
