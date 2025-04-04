
import React from 'react';

export default function Sidebar() {
  return (
    <div className="bg-panel w-80 p-4 text-sm">
      <h2 className="text-xl mb-4">Vessels</h2>
      <label className="block mb-1">ID</label>
      <select className="w-full mb-4 bg-accent text-white p-1 rounded">
        <option>Vessel 1</option>
        <option>Vessel 2</option>
      </select>
      <label className="block mb-1">Capacity</label>
      <input type="number" className="w-full mb-4 p-1 rounded bg-accent" value="200000" readOnly />
      <label className="block mb-1">Speed</label>
      <input type="range" min="10" max="25" className="w-full mb-4" />
      <label className="block mb-1">Delivery Quantity</label>
      <input type="range" min="0" max="500000" className="w-full mb-4" />
      <label className="block mb-1">Redelivery Quantity</label>
      <input type="range" min="0" max="500000" className="w-full mb-4" />
      <button className="bg-button text-white w-full py-2 rounded">Save Session</button>
    </div>
  );
}
