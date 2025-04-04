import React from 'react';
import Sidebar from './components/Sidebar';
import Map from './components/Map';
import GanttChart from './components/GanttChart';

export default function App() {
  return (
    <div className="flex h-screen w-screen">
      <Sidebar />
      <div className="flex-1 flex flex-col">
        <Map />
        <GanttChart />
      </div>
    </div>
  );
}
