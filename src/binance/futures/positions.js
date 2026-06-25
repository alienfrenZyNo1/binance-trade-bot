const BinanceFuturesPositions = {
  async getPositions() {
    const response = await this.client.get('fapi/v2/positionRisk');
    const positions = response.data;

    // Check if margin type is cross
    positions.forEach((position) => {
      if (position.marginType === 'cross') {
        // Update position margin type to cross
        position.marginType = 'cross';
        // Optionally alert if marginType != expected mode
        console.log(`Margin type for ${position.symbol} is cross, not isolated.`);
      }
    });

    return positions;
  }
};

export default BinanceFuturesPositions;