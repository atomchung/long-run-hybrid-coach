import Toybox.Lang;
import Toybox.Test;

(:test)
function zonesFollowGarminThresholds(logger) {
    var zones = [95, 114, 133, 152, 171, 190];
    Test.assert(RunMath.zoneFor(null, zones) == null);
    Test.assert(RunMath.zoneFor(140, null) == null);
    Test.assert(RunMath.zoneFor(140, [95, 114]) == null);
    Test.assert(RunMath.zoneFor(80, zones) == 1);
    Test.assert(RunMath.zoneFor(114, zones) == 1);
    Test.assert(RunMath.zoneFor(115, zones) == 2);
    Test.assert(RunMath.zoneFor(152, zones) == 3);
    Test.assert(RunMath.zoneFor(171, zones) == 4);
    Test.assert(RunMath.zoneFor(185, zones) == 5);
    Test.assert(RunMath.zoneFor(200, zones) == 5);
    return true;
}

(:test)
function numbersReadLikeTheWatch(logger) {
    Test.assert(RunMath.clock(null).equals("0:00"));
    Test.assert(RunMath.clock(65000).equals("1:05"));
    Test.assert(RunMath.clock(3738000).equals("1:02:18"));
    Test.assert(RunMath.pace(null, 1000.0).equals("--:--"));
    Test.assert(RunMath.pace(0.2, 1000.0).equals("--:--"));
    Test.assert(RunMath.pace(1000.0 / 335, 1000.0).equals("5:35"));
    Test.assert(RunMath.distance(5418.9, 1000.0).equals("5.41"));
    Test.assert(RunMath.distance(0, 1000.0).equals("0.00"));
    Test.assert(RunMath.distance(1609.344, 1609.344).equals("1.00"));
    Test.assert(RunMath.marksPassed(4999.9, 1000.0) == 4);
    Test.assert(RunMath.marksPassed(5000.0, 1000.0) == 5);
    Test.assert(RunMath.floorDiv(-30.0, 100) == -1);
    Test.assert(RunMath.floorDiv(230.0, 100) == 2);
    return true;
}

(:test)
function walkingNeedsAMedianFirst(logger) {
    var median = new CadenceMedian();
    for (var i = 0; i < 29; i += 1) {
        median.add(85);
    }
    Test.assert(!median.walking(50)); // too few moving seconds to judge
    median.add(85);
    Test.assert(median.median() == 85);
    Test.assert(median.walking(60));   // below 80% of 85
    Test.assert(!median.walking(70));  // above 80% of 85
    Test.assert(!median.walking(null));
    median.reset();
    Test.assert(median.median() == null);
    return true;
}
