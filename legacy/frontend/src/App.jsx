import React, { useState, useEffect } from 'react';
import { Activity, ShieldAlert, Cpu } from 'lucide-react';

export default function App() {
  const [decisions, setDecisions] = useState([]);

  useEffect(() => {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = window.location.port === '5173' 
        ? 'ws://localhost:8000/ws/decisions' 
        : `${protocol}//${window.location.host}/ws/decisions`;
        
    const ws = new WebSocket(wsUrl);

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      setDecisions((prev) => [data, ...prev].slice(0, 20));
    };

    return () => ws.close();
  }, []);

  return (
    <div className="min-h-screen p-8 max-w-6xl mx-auto font-sans">
      <header className="flex items-center gap-4 mb-8 border-b border-gray-700 pb-4">
        <Cpu className="text-blue-500 w-8 h-8" />
        <h1 className="text-2xl font-bold">Jev x OpenClash 决策控制台</h1>
      </header>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="col-span-1 lg:col-span-2 bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-lg">
          <h2 className="text-lg font-semibold flex items-center gap-2 mb-4">
            <Activity className="text-green-400 w-5 h-5"/> 实时切换流
          </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-gray-400 border-b border-gray-700">
                <tr>
                  <th className="pb-3 font-medium">时间</th>
                  <th className="pb-3 font-medium">选中节点</th>
                  <th className="pb-3 font-medium">置信度</th>
                  <th className="pb-3 font-medium">耗时</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-700/50">
                {decisions.map((d, i) => (
                  <tr key={i} className="hover:bg-gray-700/30 transition-colors">
                    <td className="py-3 text-gray-300">{d.timestamp.split('T')[1]}</td>
                    <td className="py-3">
                        <span className="px-2 py-1 bg-blue-500/20 text-blue-400 rounded-md border border-blue-500/30">
                            {d.selected}
                        </span>
                    </td>
                    <td className="py-3">
                      <div className="flex items-center gap-2">
                        <div className="w-16 h-2 bg-gray-700 rounded-full overflow-hidden">
                          <div className="h-full bg-green-500" style={{width: `${d.confidence * 100}%`}}></div>
                        </div>
                        <span className="text-gray-400">{d.confidence}</span>
                      </div>
                    </td>
                    <td className="py-3 text-gray-400">{d.latency_ms} ms</td>
                  </tr>
                ))}
                {decisions.length === 0 && (
                  <tr><td colSpan="4" className="py-8 text-center text-gray-500">等待内核推送数据...</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="bg-gray-800 p-6 rounded-xl border border-gray-700 shadow-lg flex flex-col">
           <h2 className="text-lg font-semibold flex items-center gap-2 mb-4">
             <ShieldAlert className="text-yellow-400 w-5 h-5"/> 最新状态上下文
           </h2>
           {decisions.length > 0 ? (
             <div className="space-y-4">
                <div className="p-4 bg-gray-900 rounded-lg border border-gray-700">
                  <div className="text-xs text-gray-500 mb-1">触发原因</div>
                  <div className="text-sm text-gray-300">{decisions[0].trigger_reason}</div>
                </div>
                <div className="p-4 bg-gray-900 rounded-lg border border-gray-700">
                  <div className="text-xs text-gray-500 mb-2">多路概率分布</div>
                  {Object.entries(decisions[0].probabilities).map(([node, prob]) => (
                    <div key={node} className="flex items-center justify-between text-sm mb-1">
                      <span className="text-gray-400 truncate w-24">{node}</span>
                      <span className="text-gray-300">{(prob * 100).toFixed(0)}%</span>
                    </div>
                  ))}
                </div>
             </div>
           ) : (
             <div className="text-sm text-gray-500 flex-1 flex items-center justify-center">暂无数据</div>
           )}
        </div>
      </div>
    </div>
  );
}
